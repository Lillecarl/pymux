"""
`unscroll-pane`, prefix U: the history comes back above the cursor until
the cursor is on the bottom row, as kitty's `scroll_prompt_to_bottom`.
"""

from __future__ import annotations

from run_commands import run_as
from session import create_session


def _shown(screen) -> list[str]:
    offset = screen.line_offset
    rows = []
    for y in range(offset, offset + screen.lines):
        row = screen.page.data_buffer.get(y, {})
        rows.append("".join(row[x].char for x in sorted(row)).rstrip())
    return rows


async def test_the_pane_shows_its_history_over_the_cursor():
    async with create_session() as (pymux, state):
        pane = pymux.arrangement.get_active_window().active_pane
        screen = pane.screen
        stream = pane.terminal.terminal_control.stream
        # More history than the screen has rows, so the cursor can reach the bottom.
        stream.feed("".join("line%d\r\n" % number for number in range(2 * screen.lines)))
        stream.feed("\x1b[2;1H\x1b[J$ ")
        top = int(_shown(screen)[0].removeprefix("line"))

        await run_as(pymux, state, "unscroll-pane")

        # The first row's line stays over the prompt, and the history fills the rest.
        assert _shown(screen)[-2:] == ["line%d" % top, "$"]
        assert _shown(screen)[0] == "line%d" % (top - screen.lines + 2)
        assert screen.pt_cursor_position.y - screen.line_offset == screen.lines - 1
