"""
Which pane may put a DEC line attribute on the wire.

`ESC # 6` draws a line of the terminal twice as wide, and the attribute
belongs to that line. A pane that holds a piece of a row cannot ask for
one: the terminal would draw the whole row wide, and the pane beside it
with it. So ptterm keeps the attribute and says nothing unless the
embedder says the pane holds whole rows, and this is where pymux
answers that.

`Pymux.pane_owns_whole_lines` is the answer. The tests below are the
three ways a row stops being this pane's own.

Lillecarl/pymux#65.
"""
from prompt_toolkit.application.current import set_app
from prompt_toolkit.application.dummy import DummyApplication
from prompt_toolkit.data_structures import Size

from pymux import arrangement
from pymux.main import Pymux


class _NoTerminal:
    "Stands in for the ptterm widget. Nothing here renders."


def a_pymux(panes: int = 1, vsplit: bool = True):
    "One server, one window, and that many panes in it."
    pymux = Pymux()
    made = []
    with set_app(DummyApplication()):
        for number in range(panes):
            pane = arrangement.Pane(_NoTerminal())
            made.append(pane)
            if number == 0:
                pymux.arrangement.create_window(pane)
            else:
                pymux.arrangement.get_active_window().add_pane(pane, vsplit=vsplit)
    return pymux, made


def owns_whole_lines(pymux, pane, columns: int = 80) -> bool:
    "What the server answers for this pane, for a client of that width."
    app = DummyApplication()
    app.output.get_size = lambda: Size(rows=40, columns=columns)
    with set_app(app):
        return pymux.pane_owns_whole_lines(pane)


def test_the_only_pane_of_a_window_holds_every_row_it_draws():
    pymux, panes = a_pymux(1)
    assert owns_whole_lines(pymux, panes[0])


def test_a_pane_beside_another_one_holds_half_of_a_row():
    pymux, panes = a_pymux(2)
    assert not owns_whole_lines(pymux, panes[0])
    assert not owns_whole_lines(pymux, panes[1])


def test_the_overlay_takes_the_rows_it_covers():
    "It floats over the layout, so no pane below it owns its own rows."
    pymux, panes = a_pymux(1)
    pymux.overlay_pane = panes[0]
    assert not owns_whole_lines(pymux, panes[0])


def test_a_client_wider_than_the_window_sees_the_background_beside_the_pane():
    """
    The window is only as wide as the narrowest client watching it.

    A wider client draws the background over the rest of every row, so
    the pane holds the left part of a row and not the row.
    """
    pymux, panes = a_pymux(1)
    assert pymux.get_window_size().columns == 80
    assert not owns_whole_lines(pymux, panes[0], columns=100)
