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
async def a_page(server, command: str, writable: bool = False):
    """
    A new session running `command`, and the demo page showing its pane
    in Chromium as the browser named "page". Answers the pane's id.
    """
    await server.cmd(["new-session", "-d", command])
    # The session this made is the one whose pane shows the row; a server
    # may start with one of its own.
    sessions = await call(server, "tree")
    pane = sessions[-1]["windows"][0]["panes"][0]["id"]
    port = a_free_port()
    token = a_token()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(lambda: serve(server.connection.socket_path, "127.0.0.1", port, token, writable))
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
    leaning. Each run is now its cells wide. Lillecarl/pymux#948.
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


async def test_a_click_on_the_page_reaches_a_program_that_asked_for_the_mouse(server):
    """
    The whole way: Chromium's click, the element's cell, the server's
    report. The tty echoes the report, so the pane shows it.
    Lillecarl/pymux#950.
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
