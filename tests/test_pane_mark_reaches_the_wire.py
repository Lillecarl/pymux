"""
The focused pane's mark reaches the wire on its first frame.

The mark lands after the copies measured their rows, one cell past
the pane on either side. A row that ends before those cells would
trim them away, so every draw that writes past the copies tells the
renderer how far it reached. A first frame with no earlier screen
paints everything it measures and nothing else, which is what makes
one frame the whole test: the mark is either in it or it is not.

The mark sits on the left pane, whose copies end long before it, so
no water covers it: only what the mark says reaches it.
"""

from __future__ import annotations

from test_focused_pane_border import create_client

#: The sides the mark always draws, whatever bars surround the pane.
MARK = "\u2503"


async def test_a_split_pane_marks_its_first_frame():
    async with create_client(["split-window -h", "select-pane -L"]) as (pymux, state, _draw):
        window = pymux.arrangement.get_active_window()
        assert len(window.panes) == 2

        # Short lines on every row: the copies end at the second
        # column. The programs are dead first, so no prompt races
        # the feed.
        for pane in window.panes:
            control = pane.terminal.terminal_control
            process = control.process
            if process is not None and not process.is_terminated:
                process.kill()
            control.stream.feed("hi\r\n" * 12)

        out = state.app.renderer.output.stdout

        # The mark stands on the left pane, at the nineteenth column:
        # only what it says reaches that far. The inner border never
        # moves while the focus does, so one frame is the whole test.
        state.app.renderer.render(state.app, state.app.layout, False)
        state.app.renderer.output.flush()
        assert MARK in out.getvalue()
