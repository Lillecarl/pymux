"""
Moving a column along the row of a strip. Lillecarl/pymux#202.

niri binds this beside the movement keys, and it is half of why the
model works: a person puts related columns next to each other without
renegotiating a layout. Without it, getting two columns adjacent means
closing one and opening it again in the right place, which is the
renegotiation a strip exists to avoid.

**Two claims, and the second is the one worth a real render.** The
order of `window.root` is a list reorder, and the tests for that need
no layout at all. What a person sees is the title bars: each one names
the panes on either side (Lillecarl/pymux#207), so a move renames
three bars, and nothing in the move says so.
"""

from test_strip_draws import CHROME, create_client
from test_the_title_bar_names_the_neighbours import (
    COLUMNS,
    create_row_of_named_panes,
    bars_of,
)

from pymux.arrangement import Pane, Window

STRIP = CHROME + ["set-window-option strip on"]


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def create_pane():
    return Pane(terminal=_Fake())


def create_strip(columns=3):
    """
    A strip of this many columns, and its panes in the order they sit
    in. The focus is on the last one, which is the one just opened.
    """
    window = Window()
    opened = [create_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(columns - 1):
        opened.append(create_pane())
        window.add_pane(opened[-1], vsplit=True)

    return window, opened


def order_of(window):
    """
    The columns of the strip, as the panes each one holds.

    A column is not always a bare pane, and a test that expected one
    would be about the shape of the tree rather than about the order.
    The first column of a strip is a stack of one: `Window.strip` wraps
    whatever the window was into the row, so the old root becomes the
    first column.
    """

    def panes_of(column):
        if isinstance(column, Pane):
            return [column]
        return [pane for item in column for pane in panes_of(item)]

    return [panes_of(column) for column in window.root]


# ----------------------------------------------------------------------
# The order of the row.


def test_a_column_moves_to_the_left():
    window, panes = create_strip(3)

    assert window.move_column(panes[2], -1) is True
    assert order_of(window) == [[panes[0]], [panes[2]], [panes[1]]]


def test_a_column_moves_to_the_right():
    window, panes = create_strip(3)

    assert window.move_column(panes[0], +1) is True
    assert order_of(window) == [[panes[1]], [panes[0]], [panes[2]]]


def test_a_column_at_the_end_of_the_row_stays_there():
    """
    And says that it did not move, rather than raising. A key held
    down at the edge of the row does nothing, the way it does nothing
    in niri.
    """
    window, panes = create_strip(3)

    assert window.move_column(panes[0], -1) is False
    assert window.move_column(panes[2], +1) is False
    assert order_of(window) == [[pane] for pane in panes]


def test_a_pane_in_a_stack_moves_the_whole_column():
    """
    The column is what moves, panes and all. Taking one pane out of a
    stack is a different move, and `break-pane` is the command for
    that kind of thing.
    """
    window, panes = create_strip(2)

    # Stack a second pane under the second column.
    stacked = create_pane()
    window.add_pane(stacked, vsplit=False)

    window.move_column(stacked, -1)

    assert order_of(window) == [[panes[1], stacked], [panes[0]]]


def test_a_column_keeps_its_width_when_it_moves():
    """
    `column_widths` is keyed by the column object, so nothing has to
    carry the width across.
    """
    window, panes = create_strip(2)
    window.switch_column_width(panes[1])
    was = window.column_width(panes[1])

    window.move_column(panes[1], -1)

    assert window.column_width(panes[1]) == was


def test_the_layout_is_rebuilt_after_a_move():
    """
    `invalidation_hash` names every pane in the order they sit in, so
    a swap of two columns that hold the same shapes still changes it.
    A hash of the shape alone would leave the panes where they were
    drawn.
    """
    window, panes = create_strip(2)
    before = window.invalidation_hash()

    window.move_column(panes[1], -1)

    assert window.invalidation_hash() != before


# ----------------------------------------------------------------------
# What a person sees.


def test_moving_a_column_renames_the_title_bars():
    """
    The bar of a pane names the panes on either side, so a move
    renames them. Nothing in the move does that: the names are read
    off the tree on every render. Lillecarl/pymux#207.
    """
    with create_client(STRIP, columns=COLUMNS) as (pymux, draw):
        panes = create_row_of_named_panes(pymux)
        moved = panes[2]

        def halves():
            bar = bars_of(pymux, draw, [moved])[0]
            return bar[: len(bar) // 2], bar[len(bar) // 2 :]

        # It sits at the end of the row, so it has beta on its left
        # and nothing on its right.
        left, right = halves()
        assert "beta" in left, (left, right)
        assert "alpha" not in left + right, (left, right)

        pymux.handle_command("move-column -L")

        # Now it is between the two, and its bar says so.
        left, right = halves()
        assert "alpha" in left, (left, right)
        assert "beta" in right, (left, right)


def test_moving_a_column_outside_a_strip_is_refused():
    """
    Every other layout divides the window, so there is no row to move
    a column along. `switch-column-width` refuses the same way.
    """
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        create_row_of_named_panes(pymux)
        draw()

        pymux.handle_command("move-column -L")

        assert "not a strip" in (pymux.get_client_state().message or ""), (
            pymux.get_client_state().message
        )
