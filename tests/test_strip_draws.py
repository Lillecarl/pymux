"""
What a strip actually puts on the screen.

`test_strip.py` judges the container on its own and `test_strip_window.py`
judges the arrangement. Neither renders pymux's real layout, and a
picture of one showed something both of them miss: **the pane title
bars were gone.**

A title bar is a `Float` at `top=-1`. It hangs one row above the pane,
in the row `_create_layout` reserves for it. A strip used to render
onto a screen of its own, where the pane's origin is row zero, so the
title bar landed on row minus one and was thrown away with that screen.
Lillecarl/pymux#161, Lillecarl/pymux#198.

These tests read the cells of the whole layout, which is the only place
that difference exists.

They also read back where the strip drew each column. That is the same
fault one layer along: the positions hang on the screen too, so a strip
that drew on its own recorded no pane anywhere, and moving between
panes did nothing.
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

from pymux.main import Pymux

ROWS, COLUMNS = 12, 40

#: A pane that stays up and draws nothing of its own, so every cell on
#: the screen is one pymux drew.
QUIET = "%s -c 'import time; time.sleep(600)'" % (sys.executable,)


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


@contextmanager
def a_client(commands=(), rows=ROWS, columns=COLUMNS):
    """
    A server with one client, and a way to draw what it draws.

    The commands run before anything is drawn, in the order given, the
    way a person would type them.
    """
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=rows, columns=columns)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=_Connection(),
        )
        try:
            with set_app(state.app):
                for command in commands:
                    pymux.handle_command(command)

                def draw():
                    """
                    Every row of the screen, as strings.

                    The screen is left where the renderer leaves its
                    own, because that is where pymux reads back the
                    positions it drew each pane at, and `select-pane
                    -L` and `-R` ask for those. A draw that skips this
                    can never see them.
                    """
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

                    # From above the screen, because a title bar is a
                    # float that hangs one row above its pane and the
                    # question is which row it landed on.
                    return {
                        y: "".join(
                            screen.data_buffer[y][x].char for x in range(columns)
                        )
                        for y in range(-2, rows)
                    }

                yield pymux, draw
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def drawn(commands=(), rows=ROWS, columns=COLUMNS):
    "Every row of the screen a client draws, as strings."
    with a_client(commands, rows, columns) as (_, draw):
        return draw()


def a_dump(rows):
    "Every row, numbered, for a test that has something to explain."
    return "\n".join("%3d %r" % (number, row) for number, row in sorted(rows.items()))


CHROME = ["set-option pane-border-status on"]


def test_a_pane_has_a_title_bar():
    "What the strip has to keep. Every other layout draws this."
    rows = drawn(CHROME)

    assert rows[0].strip(), a_dump(rows)


def test_a_strip_keeps_the_title_bar():
    """
    The one a picture found missing. The title bar is a float one row
    above the pane, and a strip draws onto a screen whose first row is
    the pane's own, so that row is off the top of it.
    """
    rows = drawn(CHROME + ["set-window-option strip on"])

    assert rows[0].strip(), a_dump(rows)


def test_a_lone_column_takes_half_the_window_and_no_more():
    """
    niri's own behaviour, and the reason a strip is not a layout: a
    column has the width it was given, and a strip of one leaves the
    rest of the screen empty rather than stretching to fill it.

    The rest belongs to no pane, so it is the background of dots that
    pymux already draws wherever a window does not reach. That is what
    niri shows beyond a half width column too.

    What must not be there is a border past the column's own. Laying the
    columns out across the whole window instead of across themselves
    filled the difference with the padding character, which drew one
    down the middle of nothing.

    **The column's share includes the border it owns**, so half of a 40
    column window is 19 cells of pane and the border at cell 19.
    Lillecarl/pymux#206.
    """
    rows = drawn(CHROME + ["set-window-option strip on"])
    share = COLUMNS // 2
    border = share - 1

    # The title bar of the one column, and then nothing.
    assert rows[0][:border].strip(), a_dump(rows)

    # The column's own right border, which the focused pane draws its
    # highlight over.
    for number in range(0, ROWS - 1):
        assert rows[number][border] != " ", a_dump(rows)

        # Beyond it, background and nothing else.
        assert set(rows[number][share:]) <= {" ", "."}, a_dump(rows)


# ----------------------------------------------------------------------
# Moving between the columns.


STRIP = CHROME + ["set-window-option strip on"]


def columns_of(pymux, how_many):
    """
    A strip of this many columns, and its panes from left to right.

    They are collected as they are opened, because `Window.panes`
    walks the root's own panes before the ones inside a stack and is
    not the order the columns are in.
    """
    window = pymux.arrangement.get_active_window()
    opened = [window.active_pane]

    for _ in range(how_many - 1):
        pymux.handle_command("split-window -h")
        opened.append(window.active_pane)

    return window, opened


def test_a_strip_records_where_it_drew_each_column():
    """
    The property `select-pane -R` needs, and the one a strip used to
    break. Moving is geometric: it steps one cell past the active
    pane's right edge and asks which pane is drawn there. So every
    column has to be recorded, in the screen's own coordinates, in the
    order they are drawn, whether or not it is on the screen.

    Three columns are wider than this window, and the focus is on the
    last of them, so the first is off the left edge. It is recorded at
    a negative position, which is what makes it reachable: a position
    off the screen is still a position.
    """
    with a_client(STRIP) as (pymux, draw):
        _, columns = columns_of(pymux, 3)
        draw()

        drawn_at = pymux.get_client_state().layout_manager.pane_write_positions
        where = [drawn_at[column] for column in columns]

        xs = [position.xpos for position in where]
        assert xs == sorted(xs), xs

        # Adjacent, with the one border cell between them.
        for left, right in zip(where, where[1:]):
            assert right.xpos == left.xpos + left.width + 1, xs

        # The first is off the left edge, and the focused one is on.
        assert xs[0] < 0, xs
        assert 0 <= xs[-1] and xs[-1] + where[-1].width <= COLUMNS, xs


def test_moving_right_reaches_the_next_column():
    """
    `select-pane -R` steps one cell past the active pane's right edge
    and looks for the pane drawn there.
    """
    with a_client(STRIP) as (pymux, draw):
        window, columns = columns_of(pymux, 2)
        draw()

        window.active_pane = columns[0]
        pymux.handle_command("select-pane -R")

        assert window.active_pane is columns[1]


def test_moving_left_comes_back():
    with a_client(STRIP) as (pymux, draw):
        window, columns = columns_of(pymux, 2)
        draw()

        pymux.handle_command("select-pane -L")

        assert window.active_pane is columns[0]


def test_moving_right_reaches_a_column_that_is_off_the_screen():
    """
    Three columns are wider than this screen, so the third one is past
    the right edge. It is drawn all the same, at a position the
    renderer never reads, and that is what makes it reachable.
    """
    with a_client(STRIP) as (pymux, draw):
        window, columns = columns_of(pymux, 3)
        draw()

        window.active_pane = columns[0]
        pymux.handle_command("select-pane -R")
        pymux.handle_command("select-pane -R")

        assert window.active_pane is columns[2]


def test_the_next_pane_still_works_in_a_strip():
    "`ctrl+b o`, which walks the panes rather than the geometry."
    with a_client(STRIP) as (pymux, draw):
        window, columns = columns_of(pymux, 2)
        draw()

        window.active_pane = columns[0]
        pymux.handle_command("select-pane -t :.+")

        assert window.active_pane is columns[1]
