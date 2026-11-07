"""
`pymux web`'s page in a real browser: what only a browser can say about
where a glyph lands. `test_the_web_server_serves.py` judges the stream
without one.

Runs in `checks.pymux-browsers`, which puts Chromium on the PATH.
"""

from __future__ import annotations

import json
import re
import socket
from contextlib import asynccontextmanager

import anyio
import pytest
from test_mcp_browsers import ARGUMENTS, CHROMIUM, call, server  # noqa: F401 -- `server` is a fixture

from pymux.web.server import a_token, serve

pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no chromium on the PATH")

#: How long one test of the page may take, from the session to the
#: browser's close. Each takes under five seconds; a hang fails here and
#: says where it waited.
PAGE_DEADLINE = 30

#: Where the border glyph stands in the row: ten narrow letters before it.
BORDER_COLUMN = 10

#: The column the border glyph stands at on the page, in cells of the
#: screen's own font. A Range and not an element: a row that is one text
#: node has no element around the glyph to measure.
WHERE_THE_BORDER_IS = """
(() => {
  const host = document.querySelector("pymux-pane");
  const screen = host.shadowRoot.querySelector(".pyte-screen");
  const walker = document.createTreeWalker(screen, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.data.indexOf("\\u2502");
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + 1);
    const probe = document.createElement("span");
    probe.textContent = "0".repeat(10);
    probe.style.position = "absolute";
    screen.append(probe);
    const cell = probe.getBoundingClientRect().width / 10;
    probe.remove();
    return (range.getBoundingClientRect().left - screen.getBoundingClientRect().left) / cell;
  }
  return null;
})()
"""


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@asynccontextmanager
async def a_page(server, command: str, writable: bool = False, session: bool = False):
    """
    A new session running `command`, and the demo page showing its pane
    in Chromium as the browser named "page". Answers the pane's id.
    `session` shows the session as a client draws it instead.
    """
    with anyio.fail_after(PAGE_DEADLINE):
        async with _a_page(server, command, writable, session) as pane:
            yield pane


@asynccontextmanager
async def _a_page(server, command: str, writable: bool, session: bool):
    await server.cmd(["new-session", "-d", command])
    # The session this made is the one whose pane shows the row; a server
    # may start with one of its own.
    sessions = await call(server, "tree")
    pane = sessions[-1]["windows"][0]["panes"][0]["id"]
    port = a_free_port()
    token = a_token()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(lambda: serve(server.connection.socket_path, "127.0.0.1", port, token, writable))
        if session:
            url = "http://127.0.0.1:%d/?session&t=%s" % (port, token)
        else:
            url = "http://127.0.0.1:%d/?pane=%s&t=%s" % (port, pane.replace("%", "%25"), token)
        await call(server, "browser.open", name="page", kind="chromium", seat="headless", url=url, arguments=ARGUMENTS)
        try:
            yield pane
        finally:
            await call(server, "browser.close", name="page")
            tasks.cancel_scope.cancel()


async def test_a_glyph_of_another_width_moves_nothing_after_it(server):
    """
    A glyph whose advance is not a cell's -- a fallback font's, here every
    glyph of a proportional one -- left the border between two panes
    leaning. Each run is now its cells wide. Lillecarl/pyterm#950.
    """
    async with a_page(server, "printf 'iiiiiiiiii\\342\\224\\202x'; sleep 60"):
        with anyio.fail_after(20):
            while await call(server, "browser.evaluate", name="page", expression=WHERE_THE_BORDER_IS) is None:
                await anyio.sleep(0.1)
        # Every glyph of a proportional font has its own width, so the
        # letters before the border are narrower than the cells they
        # stand in.
        await call(
            server,
            "browser.evaluate",
            name="page",
            expression="document.querySelector('pymux-pane').shadowRoot"
            ".querySelector('.pyte-screen').style.fontFamily = 'serif'",
        )
        column = await call(server, "browser.evaluate", name="page", expression=WHERE_THE_BORDER_IS)
        assert column == pytest.approx(BORDER_COLUMN, abs=0.05), json.dumps(column)


#: Two streams, one after the other, that give style 1 two meanings, and
#: the colour the second stream's run draws in.
TWO_STREAMS = """
(() => {
  const pane = document.createElement("pymux-pane");
  document.body.append(pane);
  const welcome = { type: "welcome", revision: 0, writable: false, css: "", size: { rows: 1, columns: 4 } };
  const frame = (colour, text) => ({
    type: "frame", revision: 1, styles: { "1": { s: "color: " + colour } }, rows: { "0": [[1, text, 1]] },
  });
  pane.receive(welcome);
  pane.receive(frame("rgb(255, 0, 0)", "a"));
  pane.receive(welcome);
  pane.receive(frame("rgb(0, 0, 255)", "b"));
  const run = pane.shadowRoot.querySelector(".row > span");
  return [run.textContent, getComputedStyle(run).color];
})()
"""


async def test_a_new_stream_draws_with_its_own_style_table(server):
    """
    A reconnect, after an upgrade or a dropped network, numbers the
    styles from one again. Lillecarl/pyterm#955.
    """
    async with a_page(server, "sleep 60"):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="!!customElements.get('pymux-pane')"
            ):
                await anyio.sleep(0.1)
        drawn = await call(server, "browser.evaluate", name="page", expression=TWO_STREAMS)
        assert drawn == ["b", "rgb(0, 0, 255)"]


async def test_the_button_fills_the_screen_with_the_pane(server):
    "Lillecarl/pyterm#953."
    async with a_page(server, "sleep 60"):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="!!document.getElementById('fullscreen')"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="#fullscreen")
        with anyio.fail_after(10):
            while (
                await call(
                    server, "browser.evaluate", name="page", expression="document.fullscreenElement?.localName ?? ''"
                )
                != "pymux-pane"
            ):
                await anyio.sleep(0.1)
        assert "refused" not in await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('about').textContent"
        )


async def test_escape_on_the_page_reaches_the_program(server):
    "Escape is pymux's abort; the page must not keep it."
    async with a_page(server, "printf ready; cat -v", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        # The click puts the keyboard on the element.
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.press", name="page", key="Escape")
        await call(server, "browser.press", name="page", key="Enter")
        with anyio.fail_after(20):
            while "^[" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)


#: The prefix, which `browser.press` cannot hold Control for. The
#: element reads the event and not who sent it. A plain character goes
#: by `beforeinput`, which only a real key fires, so it is pressed.
PRESS_WITH_CONTROL = """
document.querySelector("pymux-pane").shadowRoot.querySelector(".keyboard")
  .dispatchEvent(new KeyboardEvent("keydown", { key: %s, ctrlKey: true, bubbles: true, cancelable: true }))
"""

#: A pointer that moves across the screen, a cell at a time.
POINTER_MOVES = """
(() => {
  const screen = document.querySelector("pymux-pane").shadowRoot.querySelector(".pyte-screen");
  const box = screen.getBoundingClientRect();
  for (let x = 5; x < 200; x += 9) {
    screen.dispatchEvent(new MouseEvent("mousemove", { clientX: box.left + x, clientY: box.top + 40, bubbles: true }));
  }
})()
"""


@pytest.mark.parametrize(
    ("escape", "moving"),
    [("Escape", False), ("Escape", True), ("C-[", False)],
)
async def test_escape_in_the_session_view_leaves_copy_mode(server, escape, moving):
    """
    Escape is pymux's abort. A pointer that moves after it sends mouse
    reports right behind the escape byte. Ctrl-[ is Escape for a browser
    that keeps Escape for itself in full screen: Firefox.
    """
    async with a_page(server, "seq 100; sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")

        # Whichever session the client attached to: copy mode on any pane.
        async def mode() -> str:
            tree = await call(server, "tree")
            return "".join(each["mode"] for one in tree for window in one["windows"] for each in window["panes"])

        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key="[")
        with anyio.fail_after(20):
            while not await mode():
                await anyio.sleep(0.1)

        if escape == "C-[":
            await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("["))
        else:
            await call(server, "browser.press", name="page", key="Escape")
        if moving:
            await call(server, "browser.evaluate", name="page", expression=POINTER_MOVES)
        with anyio.fail_after(10):
            while await mode():
                await anyio.sleep(0.1)


#: Whether the element's keyboard still has the focus.
THE_KEYBOARD_HAS_THE_FOCUS = """
document.activeElement === document.querySelector("pymux-pane")
  && document.querySelector("pymux-pane").shadowRoot.activeElement?.className === "keyboard"
"""


async def test_the_escape_button_aborts_and_leaves_the_keys_on_the_terminal(server):
    """
    For a browser that keeps Escape in full screen. Chromium has the
    Keyboard Lock, so the page hides the button here; the test shows it.
    """
    async with a_page(server, "seq 100; sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        assert await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('escape').hidden"
        )
        await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('escape').hidden = false"
        )
        await call(server, "browser.click", name="page", selector="pymux-pane")

        async def mode() -> str:
            tree = await call(server, "tree")
            return "".join(each["mode"] for one in tree for window in one["windows"] for each in window["panes"])

        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key="[")
        with anyio.fail_after(20):
            while not await mode():
                await anyio.sleep(0.1)

        await call(server, "browser.click", name="page", selector="#escape")
        with anyio.fail_after(10):
            while await mode():
                await anyio.sleep(0.1)
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)


async def test_a_session_view_keeps_its_client_options_over_a_reload(server):
    "Each stream is a new client; the page names what was set on the last. Lillecarl/pyterm#959."

    async def names() -> list[str]:
        listed = await server.cmd(["list-clients", "-F", "#{client_name}"])
        return listed.stdout.split()

    async with a_page(server, "sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        # Typed on the page, as a person sets one on the client they look through.
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key=":")
        await call(
            server,
            "browser.evaluate",
            name="page",
            expression="document.querySelector('pymux-pane').send({type: 'text', text: 'set-client-option name phone'})",
        )
        await call(server, "browser.press", name="page", key="Enter")
        with anyio.fail_after(10):
            while await call(
                server, "browser.evaluate", name="page", expression="localStorage.getItem('pymux-client-options')"
            ) != json.dumps([["name", "phone"]], separators=(",", ":")):
                await anyio.sleep(0.1)

        await call(server, "browser.evaluate", name="page", expression="location.reload()")
        with anyio.fail_after(20):
            while await names() != ["phone"]:
                await anyio.sleep(0.1)


async def test_a_click_on_the_page_reaches_a_program_that_asked_for_the_mouse(server):
    """
    The whole way: Chromium's click, the element's cell, the server's
    report. The tty echoes the report, so the pane shows it.
    Lillecarl/pyterm#952.
    """
    async with a_page(server, "printf '\\033[?1000h\\033[?1006hready'; cat", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        # The welcome says whether the stream takes input; a click before
        # it is dropped.
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)

        await call(server, "browser.click", name="page", selector="pymux-pane")

        with anyio.fail_after(20):
            while not re.search(r"\[<0;\d+;\d+M", shown := await call(server, "pane.capture", pane=pane)):
                await anyio.sleep(0.1)
        assert re.search(r"\[<0;\d+;\d+m", shown), shown
