"""
A program that asks its pane to change size.

Three sequences ask: DECSLPP, and the two forms of a window resize.
xterm has one window and does as it is told. A pane does not. It sits
in a layout beside other panes, and making one taller makes another
shorter, so a program only gets its way when the person allows it with
"set-option allow-program-resize on".
"""

from pymux.arrangement import Arrangement, Pane
from pymux.layout import the_plan_of
from pymux.main import Pymux


class _FakeProcess:
    "Enough of a process to report the size of a pane."

    def __init__(self, sx, sy):
        self.sx = sx
        self.sy = sy


class _FakeTerminal:
    "Enough of a terminal for the arrangement to hold a pane."

    def __init__(self, sx=80, sy=24):
        self.process = _FakeProcess(sx, sy)


def _pymux(allow=False):
    """
    A server with two panes in one window, split top and bottom.

    Nothing here starts a process or draws anything: the resize only
    reads the size of a pane and writes the weights of a split.
    """
    pymux = Pymux()
    pymux.allow_program_resize = allow
    pymux.arrangement = Arrangement()

    first = Pane(terminal=_FakeTerminal(sy=24))
    second = Pane(terminal=_FakeTerminal(sy=24))
    pymux.arrangement.create_window(first, set_active=False)
    window = pymux.arrangement.windows[-1]
    window.add_pane(second, vsplit=False)

    return pymux, window, first, second


def _weights(window):
    "The weights of the split that holds the panes, in order."
    split = window.root
    return [split.weights[child] for child in split]


def _rows(pymux, window):
    """
    How many rows each pane really has, top to bottom.

    **The cells are what a resize is about, and the weights are how it
    is written down.** A weight is a share of a split, so a delta means
    nothing until the weights say what each pane measures now; the
    layout writes that first (`the_weights_become_the_cells`) and this
    reads what came out. Lillecarl/pymux#217.
    """
    plan = the_plan_of(pymux, window)
    return [plan.rect_of(pane).height for pane in window.panes]


def test_a_pane_keeps_its_size_while_the_option_is_off():
    pymux, window, first, _second = _pymux(allow=False)
    before = _weights(window)
    pymux.resize_pane_for_program(first, 40, None)
    assert _weights(window) == before


def test_a_taller_pane_takes_the_room_from_its_neighbour():
    pymux, window, first, _second = _pymux(allow=True)
    before = _rows(pymux, window)

    # The program holds 24 lines and asks for 30, so it asks for six
    # more than it has, and its neighbour gives up exactly those six.
    pymux.resize_pane_for_program(first, 30, None)

    after = _rows(pymux, window)
    assert after == [before[0] + 6, before[1] - 6]
    assert sum(after) == sum(before)


def test_a_shorter_pane_gives_the_room_back():
    pymux, window, first, _second = _pymux(allow=True)
    before = _rows(pymux, window)

    pymux.resize_pane_for_program(first, 20, None)

    after = _rows(pymux, window)
    assert after == [before[0] - 4, before[1] + 4]
    assert sum(after) == sum(before)


def test_a_side_the_program_left_alone_does_not_move():
    "DECSLPP names the lines, and says nothing about the columns."
    pymux, window, first, _second = _pymux(allow=True)
    before = _weights(window)
    pymux.resize_pane_for_program(first, None, None)
    assert _weights(window) == before


def test_a_pane_that_is_gone_changes_nothing():
    "A program can ask after its pane has been closed."
    pymux, window, _first, _second = _pymux(allow=True)
    before = _weights(window)
    pymux.resize_pane_for_program(Pane(terminal=_FakeTerminal()), 40, None)
    assert _weights(window) == before


def test_a_pane_never_loses_its_last_row():
    "A pane that asks for everything still leaves its neighbour a line."
    pymux, window, first, _second = _pymux(allow=True)
    pymux.resize_pane_for_program(first, 10000, None)
    assert min(_rows(pymux, window)) >= 1
