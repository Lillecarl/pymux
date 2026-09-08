"""
Moving one pane between the columns of a strip.

One key does two jobs. A pane that shares its column leaves it, into a
column of its own. A pane that is alone in its column joins the next
one. niri calls that `consume-or-expel-window-left`, and the "or" is
what makes it a key a person can hold: the pane walks in and out of the
columns and they never choose which of the two moves they meant.

The two are each other's opposite, so the tests here go one way and come
back and ask for the tree they started with. Lillecarl/pymux#213.
"""

from pymux.arrangement import HSplit, Pane, VSplit, Window


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def a_pane():
    return Pane(terminal=_Fake())


def a_strip(columns=1):
    """
    A strip with this many columns of one pane, active on the last.

    The first column is a stack of one, because turning the mode on
    wraps whatever the window was.
    """
    window = Window()
    opened = [a_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(columns - 1):
        opened.append(a_pane())
        window.add_pane(opened[-1], vsplit=True)

    return window, opened


def order_of(window):
    """
    The strip as a list of columns, each a list of its panes.

    A column is whatever holds the panes, and the first one of a strip
    is a stack of one, so the shape is what matters and not the class.
    """
    return [[pane for pane in _panes(column)] for column in window.root]


def _panes(item):
    if isinstance(item, Pane):
        return [item]
    result = []
    for child in item:
        result.extend(_panes(child))
    return result


# ----------------------------------------------------------------------
# A pane that is alone in its column joins the next one.


def test_a_lone_pane_joins_the_column_on_its_left():
    window, opened = a_strip(2)
    first, second = opened

    assert window.consume_or_expel(second, -1)

    assert order_of(window) == [[first, second]]


def test_a_lone_pane_joins_the_column_on_its_right():
    window, opened = a_strip(2)
    first, second = opened

    assert window.consume_or_expel(first, +1)

    assert order_of(window) == [[second, first]]


def test_it_joins_at_the_bottom_of_the_column():
    "One rule, so a person knows where the pane will land."
    window, opened = a_strip(3)
    first, second, third = opened
    window.consume_or_expel(second, -1)

    window.consume_or_expel(third, -1)

    assert order_of(window) == [[first, second, third]]


def test_the_strip_is_one_column_shorter_afterwards():
    window, opened = a_strip(3)

    window.consume_or_expel(opened[2], -1)

    assert len(window.root) == 2


def test_the_pane_keeps_the_focus_when_it_moves():
    window, opened = a_strip(2)
    window.active_pane = opened[1]

    window.consume_or_expel(opened[1], -1)

    assert window.active_pane is opened[1]


def test_a_lone_pane_at_the_end_of_the_row_stays():
    "A key held down at the edge does nothing, and does not raise."
    window, opened = a_strip(2)

    assert not window.consume_or_expel(opened[0], -1)
    assert order_of(window) == [[opened[0]], [opened[1]]]


def test_the_only_pane_of_the_window_stays():
    window, opened = a_strip(1)

    assert not window.consume_or_expel(opened[0], -1)
    assert not window.consume_or_expel(opened[0], +1)


def test_the_column_that_is_joined_keeps_its_width():
    """
    A column should not change size because a pane joined it. The width
    is keyed by the object that holds the column, and a bare pane
    becomes a stack, so the width has to move with it.
    """
    window, opened = a_strip(2)
    first, second = opened
    window.column_widths[window.root[0]] = 1 / 3

    window.consume_or_expel(second, -1)

    assert window.column_width(window.root[0]) == 1 / 3


# ----------------------------------------------------------------------
# A pane that shares its column leaves it.


def test_a_shared_pane_leaves_into_a_column_on_its_left():
    window, opened = a_strip(2)
    first, second = opened
    window.consume_or_expel(second, -1)

    window.consume_or_expel(second, -1)

    assert order_of(window) == [[second], [first]]


def test_a_shared_pane_leaves_into_a_column_on_its_right():
    window, opened = a_strip(2)
    first, second = opened
    window.consume_or_expel(second, -1)

    window.consume_or_expel(second, +1)

    assert order_of(window) == [[first], [second]]


def test_leaving_undoes_joining():
    "The round trip, which is what makes the key one a person holds."
    window, opened = a_strip(3)
    before = order_of(window)

    window.consume_or_expel(opened[1], -1)
    window.consume_or_expel(opened[1], +1)

    assert order_of(window) == before


def test_a_pane_may_leave_a_column_at_the_end_of_the_row():
    "There is always room for a column of its own, at either end."
    window, opened = a_strip(2)
    first, second = opened
    window.consume_or_expel(second, -1)

    assert window.consume_or_expel(first, -1)
    assert order_of(window) == [[first], [second]]


def test_the_column_it_left_collapses_to_the_pane_that_is_left():
    "A stack of one is that one, so the tree does not grow scar tissue."
    window, opened = a_strip(2)
    first, second = opened
    window.consume_or_expel(second, -1)

    window.consume_or_expel(second, +1)

    assert isinstance(window.root[0], Pane)


def test_a_pane_walks_to_the_end_of_the_row_and_comes_back():
    """
    What holding the key does. The middle pane goes left until it can
    go no further, then right the same number of times.

    The walk ends, because each move to the left either puts the pane
    in the column on its left or gives it a column of its own further
    left, and neither can go on for ever.
    """
    window, opened = a_strip(3)
    before = order_of(window)
    walker = opened[1]

    moves = 0
    while window.consume_or_expel(walker, -1):
        moves += 1

    for _ in range(moves):
        assert window.consume_or_expel(walker, +1)

    assert order_of(window) == before


# ----------------------------------------------------------------------
# The shape of the tree.


def test_the_columns_stay_the_children_of_the_root():
    "A strip is a row of columns, and nothing here may change that."
    window, opened = a_strip(3)

    window.consume_or_expel(opened[2], -1)

    assert isinstance(window.root, VSplit)
    for column in window.root:
        assert isinstance(column, (Pane, HSplit))


def test_the_panes_are_numbered_from_the_left_afterwards():
    "`Window.panes` is what a title bar and `select-pane -t` read."
    window, opened = a_strip(3)
    first, second, third = opened

    window.consume_or_expel(third, -1)

    assert window.panes == [first, second, third]
