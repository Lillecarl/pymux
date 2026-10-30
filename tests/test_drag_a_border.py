"""
Dragging the line between two panes resizes them, as tmux does with
`mouse on`. The events go through prompt_toolkit's own mouse binding
and the handlers a real frame registered. Lillecarl/pymux#772.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import apsw
import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from run_commands import run_as
from session import DEFAULT_SIZE, create_session, once, over_connection

from pymux import layout
from pymux.actions import Kind
from pymux.layout import pane_write_positions
from pymux.plan_container import LineHandler

PRESS, DRAG = 0, 32
#: A move with no button held, which any-event tracking reports.
MOVE = 35


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
    await run_as(pymux, state, command)
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
        assert isinstance(handler, LineHandler), (handler, drawn, y)
        _mouse(state, PRESS, x, y)
        _mouse(state, DRAG, x, y + 2)
        _mouse(state, PRESS, x, y + 2, "m")
        drawn = _drawn(state)

        assert (drawn[top].height, drawn[bottom].height) == (heights[0] + 2, heights[1] - 2)


async def test_a_drag_in_a_strip_widens_that_column_alone():
    "A strip column's width is its own: the next one moves along and keeps its width."
    async with create_session() as (pymux, state):
        await run_as(pymux, state, "set-window-option strip on")
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


@pytest.mark.parametrize("small", [False, True])
@pytest.mark.parametrize("strip", [False, True])
@pytest.mark.parametrize("go_left", [False, True])
async def test_a_click_in_the_other_pane_still_selects_it(strip, go_left, small):
    "The right pane's program asks for the mouse, as jjui and Claude Code do."
    async with create_session() as (pymux, state):
        if strip:
            await run_as(pymux, state, "set-window-option strip on")
        if small:
            await run_as(pymux, state, "resize-window -x 60 -y 15")
        window, left, right, drawn = await _beside(
            pymux, state, 'split-window -h "sh -c \'printf \\"\\\\033[?1003h\\\\033[?1006h\\"; sleep 30\'"'
        )
        await once(lambda: right.terminal.terminal_control.screen.sgr_mouse_support_enabled, 10.0, "no mouse mode")
        await run_as(pymux, state, "select-pane -R" if go_left else "select-pane -L")
        with set_app(state.app):
            state.sync_focus()
        target = left if go_left else right
        await run_as(pymux, state, "split-window -v -t %%%d \"sh -c 'seq 1 500; sleep 30'\"" % left.pane_id)
        (history,) = [pane for pane in window.panes if pane not in (left, right)]
        await once(lambda: history.terminal.terminal_control.screen.max_y > 400, 10.0, "no history")
        await run_as(pymux, state, "select-pane -t %%%d" % (right if go_left else left).pane_id)
        with set_app(state.app):
            state.sync_focus()
        target = history
        drawn = _drawn(state)
        x, y = drawn[target].xpos + 3, drawn[target].ypos + 2

        _mouse(state, PRESS, x, y)
        _mouse(state, PRESS, x, y, "m")

        assert window.active_pane is target


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


def _shape(shape: str) -> str:
    return "\x1b]22;%s\x1b\\" % shape


@asynccontextmanager
async def _client_with_two_panes(command: str):
    """
    A client over a connection, so a mouse report goes through its own
    parser, with two panes it has drawn. Yields what its terminal was
    sent, a cell of the line, and a way to report the pointer at a cell.
    """
    sent: list[str] = []

    def read(packet) -> None:
        if packet.get("cmd") == "out":
            sent.append(packet.get("data", ""))

    async with over_connection(read_packet=read) as session:
        state, _size = await session.attach("one", DEFAULT_SIZE)
        await run_as(session.pymux, state, command)
        await once(lambda: len(state.layout_manager.pane_write_positions) == 2, 10.0, "two panes were never drawn")
        first = min(state.layout_manager.pane_write_positions.values(), key=lambda at: (at.xpos, at.ypos))
        if "-h" in command:
            line = (first.xpos + first.width, first.ypos + 1)
        else:
            line = (first.xpos + 2, first.ypos + first.height)

        def report(code: int, x: int, y: int, end: str = "M") -> None:
            session.typed(state, "\x1b[<%d;%d;%d%s" % (code, x + 1, y + 1, end))

        yield state, sent, line, report


def _since(sent: list[str], shape: str) -> str:
    "What the terminal was sent after the last time the pointer took `shape`."
    return "".join(sent).rpartition(_shape(shape))[2]


@pytest.mark.parametrize(("command", "shape"), [("split-window -h", "col-resize"), ("split-window -v", "row-resize")])
async def test_the_pointer_takes_the_resize_shape_over_a_line(command, shape):
    "And drops it off the line. Lillecarl/pymux#819."
    async with _client_with_two_panes(command) as (_state, sent, (x, y), report):
        report(MOVE, x, y)
        await once(lambda: _shape(shape) in "".join(sent), 10.0, "the pointer never took the shape")
        report(MOVE, 1, 1)
        await once(lambda: _shape("") in _since(sent, shape), 10.0, "the pointer kept the shape off the line")


async def test_the_pointer_keeps_the_shape_while_a_drag_lasts():
    "Off the line, until the release. Lillecarl/pymux#819."
    async with _client_with_two_panes("split-window -h") as (state, sent, (x, y), report):
        report(PRESS, x, y)
        report(DRAG, x - 4, y)
        report(DRAG, x - 5, y)
        await once(
            lambda: min(at.width for at in state.layout_manager.pane_write_positions.values()) <= x - 5,
            10.0,
            "the drag never moved the line",
        )
        assert _shape("col-resize") in "".join(sent)
        assert _shape("") not in _since(sent, "col-resize")
        report(PRESS, x - 5, y, "m")
        await once(lambda: _shape("") in _since(sent, "col-resize"), 10.0, "the release kept the shape")
