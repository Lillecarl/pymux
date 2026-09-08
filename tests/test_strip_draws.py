"""
What a strip actually puts on the screen.

`test_strip.py` judges the container on its own and `test_strip_window.py`
judges the arrangement. Neither renders pymux's real layout, and a
picture of one showed something both of them miss: **the pane title
bars were gone.**

A title bar is a `Float` at `top=-1`. It hangs one row above the pane,
in the row `_create_layout` reserves for it, and drawn straight onto
the screen that row is the reserved one. A strip draws onto a screen of
its own where the pane's origin is row zero, so the title bar lands on
row minus one. Lillecarl/pymux#161, Lillecarl/pymux#198.

These tests read the cells of the whole layout, which is the only place
that difference exists.
"""

import io
import sys

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


def drawn(commands=(), rows=ROWS, columns=COLUMNS):
    """
    Every row of the screen a client draws, as strings.

    The commands run before the picture, in the order given, the way a
    person would type them.
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

                # From above the screen, because a title bar is a
                # float that hangs one row above its pane and the
                # question is which row it landed on.
                return {
                    y: "".join(screen.data_buffer[y][x].char for x in range(columns))
                    for y in range(-2, rows)
                }
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


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

    The empty half has to be empty. Laying the columns out across the
    whole window instead of across themselves filled the difference
    with the padding character, which drew a border down the middle of
    nothing.
    """
    rows = drawn(CHROME + ["set-window-option strip on"])
    edge = COLUMNS // 2

    # The title bar of the one column, and then nothing.
    assert rows[0][:edge].strip(), a_dump(rows)

    # One cell past the column is the highlight of the focused pane's
    # right border, which every layout draws at a pane's edge. Beyond
    # that there is nothing at all.
    for number in range(0, ROWS - 1):
        assert rows[number][edge] != " ", a_dump(rows)
        assert not rows[number][edge + 1 :].strip(), a_dump(rows)
