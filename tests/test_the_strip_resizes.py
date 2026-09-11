"""
A strip follows the size of the terminal. Lillecarl/pymux#206.

Carl: "The strip window mode doesn't react when i resize the outside
terminal."

A column's width is a fraction of the window, so every column has to
change when the window does. These tests hold a size that a test can
change under the client, the way a person dragging the edge of a
terminal changes it.
"""

import io
import sys
from contextlib import contextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from session import Connection
from pymux.main import Pymux

ROWS = 12
NARROW = 40
WIDE = 80

QUIET = "%s -c 'import time; time.sleep(600)'" % (sys.executable,)

STRIP = ["set-option pane-border-status on", "set-window-option strip on"]


class _Screen:
    "A terminal size a test can change."

    def __init__(self, rows, columns):
        self.size = Size(rows=rows, columns=columns)

    def get_size(self):
        return self.size


@contextmanager
def a_client(commands=(), rows=ROWS, columns=NARROW):
    "A server with one client, a way to draw, and a way to resize."
    pymux = Pymux()
    terminal = _Screen(rows, columns)
    output = Vt100_Output(stdout=io.StringIO(), get_size=terminal.get_size)

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
                    "Every row of the screen, at whatever size it is now."
                    size = terminal.get_size()
                    screen = Screen()
                    state.app.layout.container.write_to_screen(
                        screen,
                        MouseHandlers(),
                        WritePosition(
                            xpos=0, ypos=0, width=size.columns, height=size.rows
                        ),
                        "",
                        False,
                        None,
                    )
                    screen.draw_all_floats()
                    state.app.renderer._last_screen = screen
                    return screen, size

                yield pymux, terminal, draw
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def columns_of(pymux, how_many):
    "A strip of this many columns, and its panes left to right."
    window = pymux.arrangement.get_active_window()
    opened = [window.active_pane]
    for _ in range(how_many - 1):
        pymux.handle_command("split-window -h")
        opened.append(window.active_pane)
    return window, opened


def widths(pymux, panes):
    "How wide each column was drawn."
    drawn = pymux.get_client_state().layout_manager.pane_write_positions
    return [drawn[pane].width for pane in panes]


#: A column's share of the window, less the border it owns. Two default
#: columns and their two borders come to exactly the window.
#: Lillecarl/pymux#206.
def content_of(share):
    return share - 1


def test_two_default_columns_fill_the_window_exactly():
    """
    Half a window each, borders included, so the strip does not
    overflow and nothing is shaved. Lillecarl/pymux#206.
    """
    with a_client(STRIP) as (pymux, _terminal, draw):
        _window, panes = columns_of(pymux, 2)
        draw()

        drawn = widths(pymux, panes)
        assert drawn == [content_of(NARROW // 2)] * 2
        # The two columns and the two borders they own.
        assert sum(drawn) + 2 == NARROW


def test_a_wider_terminal_makes_every_column_wider():
    "The reported bug. A column is a fraction, so it has to follow."
    with a_client(STRIP) as (pymux, terminal, draw):
        _window, panes = columns_of(pymux, 2)
        draw()
        before = widths(pymux, panes)

        terminal.size = Size(rows=ROWS, columns=WIDE)
        draw()

        after = widths(pymux, panes)
        assert after == [content_of(WIDE // 2)] * 2, (before, after)


def test_a_narrower_terminal_makes_every_column_narrower():
    with a_client(STRIP, columns=WIDE) as (pymux, terminal, draw):
        _window, panes = columns_of(pymux, 2)
        draw()

        terminal.size = Size(rows=ROWS, columns=NARROW)
        draw()

        assert widths(pymux, panes) == [content_of(NARROW // 2)] * 2


def test_the_panes_are_told_the_new_size():
    """
    A column that is drawn wider has to tell the program in it, or the
    program keeps writing at the old width.
    """
    with a_client(STRIP) as (pymux, terminal, draw):
        _window, panes = columns_of(pymux, 2)
        draw()

        terminal.size = Size(rows=ROWS, columns=WIDE)
        draw()

        for pane in panes:
            assert pane.terminal.screen.columns == content_of(WIDE // 2), (
                pane.terminal.screen.columns,
            )
