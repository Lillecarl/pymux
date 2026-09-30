"""
The focused pane's border. Lillecarl/pymux#401.

`HighlightBordersIfActive` draws the mark one cell outside the pane,
so the cells it takes are shared: the row above, which is the pane's
title bar; the row below, which is the bar naming a stack's neighbours;
and the two columns beside it. The horizontals between the corners are
left to those bars, which span the same cells.

**The foot of the mark needs such a bar to sit on.** A full-height pane
has none: the row under it is the status line, and a corner there is a
stray mark on the window's chrome. So the foot is drawn only where the
pane has a bar below it, and the sides and top corners are drawn
always.
"""

import io
from contextlib import contextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from session import Connection
from pymux.layout import pane_write_positions
from pymux.main import Pymux

ROWS = 12
COLUMNS = 40


@contextmanager
def create_client(commands=(), rows=ROWS, columns=COLUMNS):
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=rows, columns=columns)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=Connection(),
        )
        try:
            with set_app(state.app):
                for command in commands:
                    pymux.handle_command(command)

                def draw():
                    screen = Screen()
                    state.app.layout.container.write_to_screen(
                        screen,
                        MouseHandlers(),
                        WritePosition(xpos=0, ypos=0, width=columns, height=rows),
                        "",
                        False,
                        None,
                    )
                    screen.draw_all_floats()
                    state.app.renderer._last_screen = screen
                    return screen

                yield pymux, state, draw
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def _char(screen, x, y) -> str:
    return screen.data_buffer[y][x].char


def _frame(screen) -> str:
    return "\n".join(
        "".join(screen.data_buffer[y][x].char for x in range(COLUMNS))
        for y in range(ROWS)
    )


def test_the_mark_closes_at_the_foot_of_a_stacked_pane():
    """
    A pane with an edge inside the window and a neighbour above has a
    column beside it and a bar row below, so all four corners are drawn
    and the two lower ones close the rectangle. They are the mark a
    stack's bottom pane was missing. Lillecarl/pymux#401.
    """
    with create_client(
        ["split-window -h", "split-window -h", "select-pane -L", "split-window"]
    ) as (pymux, state, draw):
        state.sync_focus()
        screen = draw()

        active = pymux.arrangement.get_active_window().active_pane
        at = pane_write_positions(screen)[active]

        left, right = at.xpos - 1, at.xpos + at.width
        top, foot = at.ypos - 1, at.ypos + at.height
        middle = at.ypos + at.height // 2

        assert _char(screen, left, top) == "┏"
        assert _char(screen, right, top) == "┓"
        assert _char(screen, left, foot) == "┗"
        assert _char(screen, right, foot) == "┛"
        assert _char(screen, left, middle) == "┃"
        assert _char(screen, right, middle) == "┃"


def test_the_mark_has_no_foot_at_the_windows_lower_edge():
    """
    A full-height pane has no bar row below it: the next row is the
    status line, and the lower corners would be drawn on it. The sides
    and the top corners are still drawn, so the mark is a bracket and
    not a stray character on the bar. Lillecarl/pymux#401.
    """
    with create_client(["split-window -h"]) as (pymux, state, draw):
        state.sync_focus()
        screen = draw()

        active = pymux.arrangement.get_active_window().active_pane
        at = pane_write_positions(screen)[active]

        left = at.xpos - 1
        assert _char(screen, left, at.ypos - 1) == "┏"
        for y in range(at.ypos, at.ypos + at.height):
            assert _char(screen, left, y) == "┃", (y, _char(screen, left, y))

        frame = _frame(screen)
        assert "┗" not in frame and "┛" not in frame, frame
