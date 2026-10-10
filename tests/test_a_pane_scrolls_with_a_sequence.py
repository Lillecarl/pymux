"""
A pane that scrolls one line reaches the terminal as a scroll.

prompt_toolkit's renderer sends a scroll region and "CSI S" for rows a
window proved shifted, instead of repainting them
(Lillecarl/pymux#518). At 300x90 a repaint is the whole frame, so this
is the difference between a few hundred cells and tens of thousands
of them. Lillecarl/pymux#566.
"""

from __future__ import annotations

import profile_frame
import pytest
from prompt_toolkit.application.current import set_app
from scroll_app import ALTERNATE_SCREEN, scroll_step, viewport_bytes

ROWS = 30
COLUMNS = 100


@pytest.mark.parametrize(
    "screen",
    [
        "alternate",
        pytest.param(
            "main",
            marks=pytest.mark.xfail(
                strict=True,
                reason="a slide over the history logs no scroll, Lillecarl/pymux#569",
            ),
        ),
    ],
)
async def test_a_one_line_scroll_goes_out_as_a_scroll(monkeypatch, screen):
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
            control.stream.feed(first + viewport_bytes(1, rows, columns, styled=True).decode())
            draw()

            wire = state.output.stdout
            before = wire.tell()
            control.stream.feed(scroll_step(1, +1, rows, columns, mode="scroll", styled=True).decode())
            draw()
            frame = wire.getvalue()[before:]

    assert "\x1b[S" in frame, "no scroll in a frame of %d characters" % len(frame)
    assert len(frame) < 4 * columns, "the scroll repainted %d characters" % len(frame)
