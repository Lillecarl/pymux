"""
`record-upload` sends a recording to an asciinema server, as the CLI does.

The request is the one asciinema 3.2's `src/api.rs` makes, and the files it
reads are the ones its `src/config.rs` reads. A local server stands in for
asciinema.org: nothing here reaches the network. Lillecarl/pyterm#1064.
"""

from __future__ import annotations

import base64
import email.parser
import email.policy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from run_commands import complaints, over_socket

from pymux.answer import Answer
from pymux.asciinema import Upload, UploadError, Visibility, connect_url, install_id, server_url, upload
from pymux.commands import call_command_handler
from pymux.core.main import Pymux

ID = "0b2c6a3e-5b67-4c4e-9d0b-2f5f8d7f8a11"


def homes(tmp_path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "USER": "carl"}


def test_the_server_is_the_environments_then_the_last_config_that_names_one(tmp_path):
    env = homes(tmp_path)
    system = tmp_path / "etc.toml"
    assert server_url(env, system) == "https://asciinema.org"
    system.write_text('[server]\nurl = "https://system.example"\n')
    assert server_url(env, system) == "https://system.example"
    config = tmp_path / ".config" / "asciinema"
    config.mkdir(parents=True)
    (config / "defaults.toml").write_text('[server]\nurl = "https://defaults.example/"\n')
    assert server_url(env, system) == "https://defaults.example"
    (config / "config.toml").write_text('[server]\nurl = "https://config.example"\n')
    assert server_url(env, system) == "https://config.example"
    assert server_url(env | {"ASCIINEMA_API_URL": "https://api.example"}, system) == "https://api.example"
    both = env | {"ASCIINEMA_API_URL": "https://api.example", "ASCIINEMA_SERVER_URL": "https://server.example"}
    assert server_url(both, system) == "https://server.example"


def test_the_install_id_is_the_clis_and_a_new_one_is_kept(tmp_path):
    env = homes(tmp_path)
    legacy = tmp_path / ".config" / "asciinema" / "install-id"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(ID + "\n")
    assert install_id(env) == ID

    fresh = homes(tmp_path / "fresh")
    made = install_id(fresh)
    assert (tmp_path / "fresh" / ".local" / "state" / "asciinema" / "install-id").read_text() == made
    assert install_id(fresh) == made
    assert install_id(fresh | {"ASCIINEMA_STATE_HOME": str(tmp_path / "own")}) != made


def test_the_connect_link_names_the_install_id(tmp_path):
    env = homes(tmp_path) | {"ASCIINEMA_SERVER_URL": "http://example.test"}
    assert connect_url(env) == "http://example.test/connect/%s" % install_id(env)


def a_form(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    "Each field of a multipart body: its file name, if any, and its bytes."
    head = b"Content-Type: %s\r\n\r\n" % request.headers["content-type"].encode()
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + request.content)
    return {
        part.get_param("name", header="content-disposition"): (part.get_filename(), part.get_payload(decode=True))
        for part in message.iter_parts()
    }


async def test_an_upload_is_the_request_the_cli_makes(tmp_path):
    env = homes(tmp_path) | {"ASCIINEMA_SERVER_URL": "http://example.test"}
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"url": "http://example.test/a/1"})

    what = Upload("{}\n", "demo.cast", title="A demo", visibility=Visibility.UNLISTED)
    url = await upload(what, env, "pymux/test", httpx.MockTransport(answer))
    assert url == "http://example.test/a/1"
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/api/v1/recordings")
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"carl:" + install_id(env).encode()).decode()
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "pymux/test"
    assert a_form(request) == {
        "file": ("demo.cast", b"{}\n"),
        "title": (None, b"A demo"),
        "visibility": (None, b"unlisted"),
    }


@pytest.mark.parametrize(
    ("status", "body", "says"),
    [
        (401, {"message": "Unauthorized"}, "record-auth"),
        (413, None, "size limit"),
        (422, {"type": "validation_failed", "message": "Invalid recording"}, "Invalid recording"),
    ],
)
async def test_a_refusal_says_why(tmp_path, status, body, says):
    env = homes(tmp_path) | {"ASCIINEMA_SERVER_URL": "http://example.test"}

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body) if body else httpx.Response(status)

    with pytest.raises(UploadError, match=says) as raised:
        await upload(Upload("{}\n", "demo.cast"), env, "pymux/test", httpx.MockTransport(answer))
    assert install_id(env) not in str(raised.value)


async def test_record_auth_prints_the_link_to_the_caller_alone(tmp_path, monkeypatch):
    "The link holds the install id, so a key binding, which shows on a screen, gets none."
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("ASCIINEMA_SERVER_URL", "http://example.test")
    mux = Pymux()
    async with mux.running():
        assert complaints(mux, "record-auth") == [
            "pymux: record-auth: run it from a shell, so the link is not shown on a screen"
        ]
        assert not (tmp_path / ".local").exists()
        mux.answer = answer = Answer()
        try:
            assert call_command_handler("record-auth", mux, []) is None
        finally:
            mux.answer = None
    assert answer.output == [connect_url({"HOME": str(tmp_path), "ASCIINEMA_SERVER_URL": "http://example.test"})]


class FakeAsciinema(BaseHTTPRequestHandler):
    "asciinema.org's upload, as much of it as an upload reads."

    seen: list[tuple[str, dict[str, str], bytes]]

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["content-length"]))
        self.seen.append((self.path, dict(self.headers), body))
        reply = json.dumps({"url": "http://%s:%d/a/7" % self.server.server_address[:2]}).encode()
        self.send_response(201)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def fake_asciinema():
    seen: list = []
    handler = type("Handler", (FakeAsciinema,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


async def test_record_upload_sends_the_recording_without_its_keys(tmp_path, monkeypatch, fake_asciinema):
    url, seen = fake_asciinema
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("ASCIINEMA_SERVER_URL", url)
    # The build sandbox names a CA file that is not there, and httpx reads it.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    recordings = tmp_path / "state" / "pymux" / "recordings"
    recordings.mkdir(parents=True)
    events = [
        [0.0, "out", base64.b64encode(b"$ ").decode()],
        [0.5, "in", base64.b64encode(b"hunter2\r").decode()],
        [0.6, "out", base64.b64encode(b"***\r\n").decode()],
    ]
    (recordings / "demo.session.json").write_text(json.dumps({"lines": 24, "columns": 80, "events": events}))

    mux = Pymux()
    async with mux.running():
        mux.answer = answer = Answer()
        try:
            waiting = call_command_handler("record-upload", mux, ["-T", "Demo", "demo"])
            assert waiting is not None
            await waiting
        finally:
            mux.answer = None
        assert await over_socket(mux, "record-upload", "missing") == [
            "pymux: record-upload: no recording %s" % (recordings / "missing.session.json")
        ]

    assert answer.errors == []
    assert answer.output == [url + "/a/7"]
    ((path, headers, body),) = seen
    assert path == "/api/v1/recordings"
    assert headers["Authorization"].startswith("Basic ")
    assert b"hunter2" not in body
    assert b'filename="demo.cast"' in body
    assert b"***" in body
