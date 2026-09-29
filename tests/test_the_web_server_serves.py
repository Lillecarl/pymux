"""
`pymux web`, driven the way a browser drives it.

`tests/test_the_web_server.py` asks what the adapter holds; this asks what
it does, over a real websocket to a real pymux server. It would have caught
both of the faults that building it by hand found, and neither was the kind
a unit test sees:

- the element and the page were not in the wheel, so the serving answered
  nothing and said nothing about why;
- the token went into a buffered stdout, so it reached nobody until the
  process that needed it had ended.

**No browser here.** Whether the page draws is what a browser answers and
nothing else can, which is why a headless one runs against this separately.
Lillecarl/pymux#461.
"""

import json
import sys

import anyio
import pytest
from websockets.asyncio.client import connect

from pymux.main import Pymux
from pymux.web.server import a_token, serve

LINES = 6
COLUMNS = 30

#: A program that stays until something kills it. `python -c pass` ends
#: before the first frame, and a pane that is already dead says nothing
#: about a stream of one that is not.
STAYS = "%s -c \"import time; time.sleep(60)\"" % (sys.executable,)


@pytest.fixture
def pymux(tmp_path):
    mux = Pymux()
    mux.test_mode = True
    mux.create_window(STAYS)
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    mux.listen_on_socket(str(tmp_path / "s.sock"))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for held in list(window.panes):
                process = getattr(held, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


class _Serving:
    "A `pymux web` on a port of its own, and the token it wants."

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def url(self, pane_id: str, token: str | None = None) -> str:
        return "ws://127.0.0.1:%d/pane/%s?t=%s" % (
            self.port,
            pane_id,
            self.token if token is None else token,
        )


async def _serving(mux, tasks, writable: bool) -> _Serving:
    "Start the adapter beside that server and wait for the port."
    token = a_token()
    # Port 0 would be the tidy answer, and `websockets` does not report
    # back which one it took through `serve`. So a port is chosen and the
    # wait below is what says it came up.
    port = 18100 + (hash(mux.socket_name) % 400)

    tasks.start_soon(
        lambda: serve(mux.socket_name, "127.0.0.1", port, token, writable)
    )

    with anyio.fail_after(10):
        while True:
            try:
                stream = await anyio.connect_tcp("127.0.0.1", port)
            except OSError:
                await anyio.sleep(0.05)
            else:
                await stream.aclose()
                return _Serving(port, token)


def pane_id(mux) -> str:
    return "%%%i" % (mux.arrangement.get_active_window().active_pane.pane_id,)


async def test_a_viewer_with_the_token_is_sent_the_pane(pymux):
    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            async with connect(web.url(pane_id(pymux))) as socket:
                welcome = json.loads(await socket.recv())
                assert welcome["type"] == "welcome"
                assert welcome["writable"] is False
                assert welcome["size"] == {"columns": COLUMNS, "rows": LINES}
                # The stylesheet travels with the stream, because a viewer
                # has no second route to the server.
                assert "--pyte-1:" in welcome["css"]

                first = json.loads(await socket.recv())
                assert first["type"] == "frame"
                assert first["whole"] is True
                assert len(first["rows"]) == LINES

            tasks.cancel_scope.cancel()
        pymux.stop()


@pytest.mark.parametrize("token", ["", "not-the-token"])
async def test_a_viewer_without_the_token_is_refused(pymux, token):
    """
    **This is the guard, so it is the test that matters most here.** A
    browser sends no credentials on a websocket upgrade, so on loopback an
    `Origin` check is the only other thing and a page may simply omit the
    header.
    """
    from websockets.exceptions import InvalidStatus

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            with pytest.raises(InvalidStatus) as refused:
                async with connect(web.url(pane_id(pymux), token=token)):
                    pass

            assert refused.value.response.status_code == 403

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_writable_viewer_types_into_the_pane(pymux):
    pane = pymux.arrangement.get_active_window().active_pane
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)

            async with connect(web.url(pane_id(pymux))) as socket:
                assert json.loads(await socket.recv())["writable"] is True
                await socket.recv()

                await socket.send(json.dumps({"type": "text", "text": "hi"}))
                with anyio.fail_after(5):
                    while not written:
                        await anyio.sleep(0.02)

            assert written == ["hi"]

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_showing_viewer_cannot_type(pymux):
    "`--allow-input` is off by default, and the server is what enforces it."
    pane = pymux.arrangement.get_active_window().active_pane
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            async with connect(web.url(pane_id(pymux))) as socket:
                await socket.recv()
                await socket.recv()
                await socket.send(json.dumps({"type": "text", "text": "hi"}))
                await anyio.sleep(0.2)

            assert written == []

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_the_page_and_the_element_are_served(pymux):
    """
    Over HTTP on the same port, which is the whole of what a browser needs
    before a websocket means anything. The wheel not carrying these is the
    fault this catches.
    """
    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            for path, wanted in [
                ("/", b"<pymux-pane"),
                ("/pymux-pane.js", b"customElements.define"),
                ("/page.js", b"pymux-pane.js"),
                ("/page.css", b"pymux-pane"),
            ]:
                head, body = await _http(web.port, path)
                assert b"200" in head, (path, head)
                assert wanted in body, path

            # And the policy the element is written to need nothing beyond.
            head, _body = await _http(web.port, "/")
            assert b"default-src 'self'" in head

            tasks.cancel_scope.cancel()
        pymux.stop()


async def _http(port: int, path: str):
    "One GET, without a client library: the answer's head and body."
    stream = await anyio.connect_tcp("127.0.0.1", port)
    async with stream:
        await stream.send(
            ("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n" % path)
            .encode()
        )
        said = b""
        with anyio.move_on_after(5):
            while True:
                try:
                    said += await stream.receive()
                except anyio.EndOfStream:
                    break
    head, _, body = said.partition(b"\r\n\r\n")
    return head, body
