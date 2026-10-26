"""
Dragging the line between two panes resizes them, as tmux does with
`mouse on`. The events go through prompt_toolkit's own mouse binding
and the handlers a real frame registered. Lillecarl/pymux#772.
"""

from __future__ import annotations

import apsw
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from session import create_session
from test_click_selects_pane import run

from pymux import layout
from pymux.actions import Kind
from pymux.layout import pane_write_positions

PRESS, DRAG = 0, 32


def _drawn(state) -> dict:
    "Draw a frame, and say where each pane went."
    with set_app(state.app):
        state.layout_manager.frame_started(state.app)
        state.app.renderer.render(state.app, state.app.layout)
        state.layout_manager.frame_ended(state.app)
    return dict(pane_write_positions(state.app.renderer._last_screen))


def _mouse(state, code: int, x: int, y: int, end: str = "M") -> None:
    "One SGR mouse report, at a cell of the screen."
    with set_app(state.app):
        state.app.key_processor.feed(KeyPress(Keys.Vt100MouseEvent, "\x1b[<%d;%d;%d%s" % (code, x + 1, y + 1, end)))
        state.app.key_processor.process_keys()


async def _beside(pymux, state, command="split-window -h"):
    await run(pymux, state, command)
    window = pymux.arrangement.get_active_window()
    first, second = window.panes
    return window, first, second, _drawn(state)


async def test_a_drag_moves_the_line_between_two_columns(tmp_path):
    async with create_session() as (pymux, state):
        pymux.actions.path = tmp_path / "actions.db"
        pymux.options.action_log = True
        _, left, right, drawn = await _beside(pymux, state)
        widths = drawn[left].width, drawn[right].width
        x, y = drawn[left].xpos + drawn[left].width, drawn[left].ypos + 1

        _mouse(state, PRESS, x, y)
        # Off the line and over the left pane, before a frame is drawn.
        _mouse(state, DRAG, x - 3, y)
        _drawn(state)
        handler = state.app.renderer.mouse_handlers.mouse_handlers[y + 2][x - 5]
        assert getattr(handler, "func", handler).__name__ == "_follow", (handler, state.border_drag)
        # After one: the float over everything takes it.
        _mouse(state, DRAG, x - 5, y + 2)
        _mouse(state, PRESS, x - 5, y + 2, "m")
        drawn = _drawn(state)

        assert (drawn[left].width, drawn[right].width) == (widths[0] - 5, widths[1] + 5)
        assert state.border_drag is None
        await pymux.actions.settled()
        db = apsw.Connection(str(pymux.actions.path))
        try:
            rows = list(db.execute("SELECT kind, name, origin FROM actions WHERE kind = ?", (Kind.CLICK,)))
        finally:
            db.close()
        assert rows == [(Kind.CLICK, "drag-border", "mouse")]


async def test_a_drag_moves_the_line_between_two_rows():
    async with create_session() as (pymux, state):
        _, top, bottom, drawn = await _beside(pymux, state, "split-window -v")
        heights = drawn[top].height, drawn[bottom].height
        x, y = drawn[top].xpos + 2, drawn[top].ypos + drawn[top].height

        handler = state.app.renderer.mouse_handlers.mouse_handlers[y][x]
        assert getattr(handler, "func", handler).__name__ == "press_on_line", (handler, drawn, y)
        _mouse(state, PRESS, x, y)
        _mouse(state, DRAG, x, y + 2)
        _mouse(state, PRESS, x, y + 2, "m")
        drawn = _drawn(state)

        assert (drawn[top].height, drawn[bottom].height) == (heights[0] + 2, heights[1] - 2)


async def test_a_drag_in_a_strip_widens_that_column_alone():
    "A strip column's width is its own: the next one moves along and keeps its width."
    async with create_session() as (pymux, state):
        await run(pymux, state, "set-window-option strip on")
        _, left, right, drawn = await _beside(pymux, state)
        widths = drawn[left].width, drawn[right].width
        x, y = drawn[left].xpos + drawn[left].width, drawn[left].ypos + 1

        _mouse(state, PRESS, x, y)
        _mouse(state, DRAG, x - 6, y)
        _mouse(state, PRESS, x - 6, y, "m")
        drawn = _drawn(state)

        assert (drawn[left].width, drawn[right].width) == (widths[0] - 6, widths[1])


async def test_a_pane_keeps_a_cell_however_far_the_drag_goes():
    async with create_session() as (pymux, state):
        _, left, right, drawn = await _beside(pymux, state)
        x, y = drawn[left].xpos + drawn[left].width, drawn[left].ypos + 1

        _mouse(state, PRESS, x, y)
        _mouse(state, DRAG, 0, y)
        _mouse(state, PRESS, 0, y, "m")
        drawn = _drawn(state)

        assert drawn[left].width == 1


async def test_a_move_with_no_drag_does_nothing():
    "The terminal reports every move, and a move over a line is not a press."
    async with create_session() as (pymux, state):
        _, left, right, drawn = await _beside(pymux, state)
        x, y = drawn[left].xpos + drawn[left].width, drawn[left].ypos + 1

        _mouse(state, DRAG, x, y)
        _mouse(state, DRAG, x - 4, y)
        assert state.border_drag is None
        assert _drawn(state)[left].width == drawn[left].width


async def test_a_window_pane_hears_its_size_at_the_release(monkeypatch):
    "Each size is a Wayland configure, and the program draws for each."
    async with create_session() as (pymux, state):
        _, left, right, drawn = await _beside(pymux, state)
        told = []
        # The terminal stands in for a window pane: it is drawn the same.
        monkeypatch.setattr(layout, "WindowWidget", type(left.terminal))
        monkeypatch.setattr(left.terminal, "set_size", lambda width, height: told.append((width, height)))
        x, y = drawn[left].xpos + drawn[left].width, drawn[left].ypos + 1

        _mouse(state, PRESS, x, y)
        told.clear()
        for step in range(1, 4):
            _mouse(state, DRAG, x - step, y)
            _drawn(state)
        assert told == []

        _mouse(state, PRESS, x - 3, y, "m")
        _drawn(state)
        assert told[-1][0] == drawn[left].width - 3
