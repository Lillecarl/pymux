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
Lillecarl/pyterm#463.
"""

from __future__ import annotations

import json
import re
import socket
import sys

import anyio
import pytest
from fairclock import anyio as fair_anyio
from websockets.asyncio.client import connect

from pymux.core.main import Pymux
from pymux.web.server import GUARDS, STATIC, a_token, serve

LINES = 6
COLUMNS = 30

#: A program that stays until something kills it. `python -c pass` ends
#: before the first frame, and a pane that is already dead says nothing
#: about a stream of one that is not.
STAYS = '%s -c "import time; time.sleep(60)"' % (sys.executable,)


@pytest.fixture
async def pymux(tmp_path):
    mux = Pymux()
    mux.options.test_mode = True
    async with mux.running():
        await mux.panes.create_window(STAYS)
        pane = mux.arrangement.get_active_window().active_pane
        pane.screen.resize(LINES, COLUMNS)
        mux.serving.listen_on_socket(str(tmp_path / "s.sock"))
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
    # `serve` does not report the port it took, so the kernel picks a free
    # one here and the wait below says it came up. A port hashed from the
    # socket name, one of 400, collided between tests run in parallel.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    tasks.start_soon(lambda: serve(mux.socket_name, "127.0.0.1", port, token, writable))

    with fair_anyio.fail_after(10):
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


async def test_a_viewer_of_the_session_is_a_client_of_it(pymux):
    """
    `/session` is what one client draws: the panes, and the status line a
    pane stream has none of. The size is the viewer's, because a client's
    size is its terminal's. Lillecarl/pyterm#483.
    """
    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)
            url = "ws://127.0.0.1:%d/session?t=%s&rows=10&columns=40" % (
                web.port,
                web.token,
            )

            async with connect(url) as socket:
                welcome = json.loads(await socket.recv())
                assert welcome["type"] == "welcome"
                assert welcome["size"] == {"columns": 40, "rows": 10}
                assert welcome["writable"] is False

                rows: dict = {}
                with fair_anyio.fail_after(10):
                    while not rows.get("9", "").strip():
                        frame = json.loads(await socket.recv())
                        for number, runs in frame.get("rows", {}).items():
                            rows[number] = "".join(text for _style, text, _cells in runs)

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_notification_reaches_the_session_view_and_its_click_shows_the_pane(pymux):
    """
    The session view's terminal answers OSC 99's query as kitty does, so
    the server sends it a pane's notification, and the page's click
    shows that pane to the view's client. Lillecarl/pyterm#1073.
    """
    there = pymux.arrangement.get_active_window().active_pane
    async with pymux.running():
        await pymux.panes.create_window(STAYS)
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)
            url = "ws://127.0.0.1:%d/session?t=%s&rows=10&columns=40" % (web.port, web.token)

            async with connect(url) as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"
                await socket.send(json.dumps({"type": "focus", "focused": False}))

                def the_view():
                    "The view's client and its state, once it attached."
                    return next(
                        ((client, state) for client, state in pymux._client_states.items() if not state.temporary),
                        (None, None),
                    )

                def shown(state):
                    return state.session.arrangement.get_active_window_for(state.app).panes

                # The answer to the query arrives with the detection.
                with fair_anyio.fail_after(10):
                    while (view := the_view())[0] is None or not view[0].replies.notification_osc:
                        await socket.recv()
                _client, state = view
                assert there not in shown(state)

                pymux.notification_center.add("Build done", "all green", pane_id=there.pane_id)
                with fair_anyio.fail_after(10):
                    while (message := json.loads(await socket.recv()))["type"] != "notification":
                        pass
                assert (message["title"], message["body"]) == ("Build done", "all green")

                await socket.send(json.dumps({"type": "notification-click", "id": message["id"]}))
                with fair_anyio.fail_after(10):
                    while there not in shown(state):
                        await socket.recv()

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_web_page_tells_the_page_of_a_session_view_to_act(pymux):
    "Only a web page hears it; with none attached the command says so. Lillecarl/pyterm#1076."
    from libpymux.connection import CommandError
    from libpymux.objects import Server

    async with pymux.running():
        with pytest.raises(CommandError, match="no web page is attached"):
            await Server(pymux.socket_name).cmd(["web-page", "reload"])

        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)
            url = "ws://127.0.0.1:%d/session?t=%s&rows=10&columns=40" % (web.port, web.token)
            async with connect(url) as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"
                with fair_anyio.fail_after(10):
                    while not any(state.client.web_page for state in pymux.clients):
                        await anyio.sleep(0.02)
                said = await Server(pymux.socket_name).cmd(["web-page", "notifications"])
                assert said.exit_code == 0, said
                with fair_anyio.fail_after(10):
                    while (message := json.loads(await socket.recv()))["type"] != "page":
                        pass
                assert message == {"type": "page", "action": "notifications"}

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_viewer_of_the_session_without_the_token_is_refused(pymux):
    from websockets.exceptions import InvalidStatus

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            with pytest.raises(InvalidStatus) as refused:
                async with connect("ws://127.0.0.1:%d/session?t=wrong" % web.port):
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
                with fair_anyio.fail_after(5):
                    while not written:
                        await anyio.sleep(0.02)

            assert written == ["hi"]

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_probe_event_types_nothing_and_keeps_the_stream(pymux):
    "`?probe` sends each keyboard event for the output to show. Lillecarl/pyterm#1069."
    pane = pymux.arrangement.get_active_window().active_pane
    written = []
    pane.process.write_input = written.append

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)

            async with connect(web.url(pane_id(pymux))) as socket:
                await socket.recv()
                await socket.recv()
                await socket.send(json.dumps({"type": "probe", "event": {"type": "compositionend", "data": "Can"}}))
                await socket.send(json.dumps({"type": "text", "text": "hi"}))
                with fair_anyio.fail_after(5):
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
                # The element imports this, so a browser asks for it
                # next and a 404 here means nothing is ever defined.
                ("/keys.js", b"keysFor"),
                ("/retry.js", b"retryAfter"),
                ("/page.js", b"pymux-pane.js"),
                ("/page.css", b"pymux-pane"),
                # What shows a notification on a phone. Lillecarl/pyterm#1073.
                ("/sw.js", b"notificationclick"),
            ]:
                head, body = await _http(web.port, path)
                assert b"200" in head, (path, head)
                assert wanted in body, path

            # And the policy the element is written to need nothing beyond.
            head, _body = await _http(web.port, "/")
            assert b"default-src 'self'" in head

            # Every answer carries the guards, a refusal too.
            for path in ["/", "/nothing-here", "/session?t=wrong"]:
                head, _body = await _http(web.port, path)
                for name, value in GUARDS.items():
                    assert ("%s: %s" % (name, value)).encode() in head, (path, name)

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_the_fonts_the_sheet_names_are_served_and_kept(pymux):
    """
    The page draws with the fonts a picture of a pane does, from this
    server: a phone has none of them. The files carry their hash in the
    name, so a browser keeps them. Lillecarl/pyterm#1070.
    """
    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=False)

            head, sheet = await _http(web.port, "/fonts.css")
            assert b"200" in head, head
            named = re.findall(rb'url\("(fonts/[^"]+)"\)', sheet)
            if (STATIC / "fonts").is_dir():
                assert named and b"--pyte-font:" in sheet
            for path in named:
                head, body = await _http(web.port, "/" + path.decode())
                assert b"200" in head and b"Content-Type: font/woff2" in head, (path, head)
                assert b"immutable" in head and body[:4] == b"wOF2", path

            for path in ["/fonts/../index.html", "/fonts/x.woff2", "/fonts/fonts.css"]:
                head, _body = await _http(web.port, path)
                assert b"404" in head, (path, head)

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_a_page_on_another_site_is_refused_even_with_the_token(pymux):
    """
    A browser sends `Origin` on every upgrade, so this is what stops a
    page on another site the moment a cookie stands in for the token.
    """
    from websockets.exceptions import InvalidStatus

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)

            with pytest.raises(InvalidStatus) as refused:
                async with connect(web.url(pane_id(pymux)), origin="https://elsewhere.example"):
                    pass
            assert refused.value.response.status_code == 403

            # The server's own page is its own origin.
            async with connect(web.url(pane_id(pymux)), origin="http://127.0.0.1:%d" % web.port) as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"

            tasks.cancel_scope.cancel()
        pymux.stop()


async def test_text_that_is_not_json_closes_the_stream_with_the_reason(pymux):
    from websockets.exceptions import ConnectionClosed

    async with pymux.running():
        async with anyio.create_task_group() as tasks:
            web = await _serving(pymux, tasks, writable=True)

            async with connect(web.url(pane_id(pymux))) as socket:
                await socket.recv()
                await socket.send("{not json")
                with fair_anyio.fail_after(5), pytest.raises(ConnectionClosed) as closed:
                    while True:
                        await socket.recv()
            assert closed.value.rcvd is not None
            assert (closed.value.rcvd.code, closed.value.rcvd.reason) == (1003, "that is not JSON")

            tasks.cancel_scope.cancel()
        pymux.stop()


async def _http(port: int, path: str):
    "One GET, without a client library: the answer's head and body."
    stream = await anyio.connect_tcp("127.0.0.1", port)
    async with stream:
        await stream.send(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n" % path).encode())
        said = b""
        with anyio.move_on_after(5):
            while True:
                try:
                    said += await stream.receive()
                except anyio.EndOfStream:
                    break
    head, _, body = said.partition(b"\r\n\r\n")
    return head, body
