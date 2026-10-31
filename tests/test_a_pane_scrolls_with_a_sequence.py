"""
A pane that scrolls one line reaches the terminal as a scroll.

prompt_toolkit's renderer sends a scroll region and "CSI S" for rows a
window proved shifted, instead of repainting them
(Lillecarl/pymux#518). At 300x90 a repaint is the whole frame, so this
is the difference between a few hundred cells and tens of thousands
of them. Lillecarl/pymux#566.

The main screen moves no rows: it slides over its history, and the
window says so by how far its view moved. Lillecarl/pymux#569.
"""

from __future__ import annotations

import profile_frame
import pytest
from prompt_toolkit.application.current import set_app
from scroll_app import ALTERNATE_SCREEN, scroll_step, viewport_bytes

ROWS = 30
COLUMNS = 100


#: Where the document starts, so a step up has lines above it.
TOP = 10
#: The scroll each direction goes out as.
SCROLLS = {+1: "\x1b[S", -1: "\x1b[T"}


async def _frames(monkeypatch, screen: str, mode: str, direction: int, pieces: int = 1) -> list[str]:
    """
    What the terminal reads for one step, a frame per piece.

    `pieces` cuts the step's bytes apart with a frame drawn after each,
    the way a pty hands a big repaint to a pane in reads of its own.
    """
    monkeypatch.setattr(profile_frame, "ROWS", ROWS)
    monkeypatch.setattr(profile_frame, "COLUMNS", COLUMNS)
    async with profile_frame.server(1) as (pymux, state):
        with set_app(state.app):
            draw = profile_frame.create_frame(state)
            draw()
            control = pymux.arrangement.get_active_window().panes[0].terminal.terminal_control
            rows = control.screen.lines
            columns = control.screen.columns
            first = ALTERNATE_SCREEN if screen == "alternate" else ""
            control.stream.feed(first + viewport_bytes(TOP, rows, columns, styled=True).decode())
            draw()

            wire = state.output.stdout
            step = scroll_step(TOP, direction, rows, columns, mode=mode, styled=True).decode()
            size = -(-len(step) // pieces)
            frames = []
            for start in range(0, len(step), size):
                before = wire.tell()
                control.stream.feed(step[start : start + size])
                draw()
                frames.append(wire.getvalue()[before:])
    return frames


@pytest.mark.parametrize("direction", [+1, -1])
@pytest.mark.parametrize(
    ("screen", "mode"),
    [
        ("alternate", "scroll"),
        ("main", "scroll"),
        # The whole viewport written again, one line further on: the
        # pane finds the rows by their images. Lillecarl/pymux#570.
        ("alternate", "redraw"),
    ],
)
async def test_a_one_line_scroll_goes_out_as_a_scroll(monkeypatch, screen, mode, direction):
    (frame,) = await _frames(monkeypatch, screen, mode, direction)

    assert SCROLLS[direction] in frame, "no scroll in a frame of %d characters" % len(frame)
    assert len(frame) < 4 * COLUMNS, "the scroll repainted %d characters" % len(frame)


@pytest.mark.parametrize("direction", [+1, -1])
async def test_a_redraw_read_in_pieces_still_scrolls(monkeypatch, direction):
    """
    A repaint that reaches the pane in four reads draws four frames, and
    each one scrolls the rows it rewrote. Measured at 300x90 by
    `checks.pymux-scrolling`: down sent 2323 bytes a step, up 7042,
    because a piece that ends inside a row leaves the cursor on it, and
    what the pane remembered drawing there was a line the terminal no
    longer showed. Lillecarl/pymux#512.
    """
    frames = await _frames(monkeypatch, "alternate", "redraw", direction, pieces=4)

    sent = sum(len(frame) for frame in frames)
    assert sent < 4 * 4 * COLUMNS, "the pieces repainted %d characters: %r" % (sent, [len(f) for f in frames])
