"""
What a split does inside a strip.

A horizontal split opens a new column beside the one a person is on,
rather than dividing the pane they are on. That is the whole of what
the mode is for: the panes already open keep their widths, and the row
grows past the edge of the screen instead of everything getting
narrower.

A vertical split still stacks inside the column, which is what a niri
column holds.

**Outside a strip nothing changes.** The last two tests make the same
calls on a window that is not a strip and get the old answers, because
a strip is one window's option and not a new way for pymux to work.

Lillecarl/pymux#198.
"""

from pymux.arrangement import DEFAULT_COLUMN_WIDTH, HSplit, Pane, VSplit, Window


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def a_pane():
    return Pane(terminal=_Fake())


def a_strip(columns=1):
    """
    A strip with this many columns, active on the last.

    The panes come back in the order they were opened, which is the
    order of the columns. `Window.panes` is in that order too now
    (Lillecarl/pymux#210), and this still collects them as they are
    opened: a test about where a split puts a pane should not be
    reading its answer out of the thing it is judging.
    """
    window = Window()
    opened = [a_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(columns - 1):
        opened.append(a_pane())
        window.add_pane(opened[-1], vsplit=True)

    return window, opened


def shape(item):
    "The tree, as nested lists naming each kind."
    if isinstance(item, VSplit):
        return ["V"] + [shape(one) for one in item]
    if isinstance(item, HSplit):
        return ["H"] + [shape(one) for one in item]
    return "p"


# ----------------------------------------------------------------------
# A horizontal split opens a column.


def test_a_horizontal_split_opens_a_column():
    window, _ = a_strip(1)

    window.add_pane(a_pane(), vsplit=True)

    assert shape(window.root) == ["V", ["H", "p"], "p"]


def test_the_column_opens_beside_the_one_a_person_is_on():
    "Not at the end of the strip: beside the pane they were looking at."
    window, opened = a_strip(3)
    window.active_pane = opened[0]

    window.add_pane(a_pane(), vsplit=True)

    # The first column is the `HSplit` the window started as, so the
    # new column is the one after it.
    assert window.root.index(window.active_pane) == 1


def test_the_new_column_takes_the_focus():
    window, _ = a_strip(1)
    fresh = a_pane()

    window.add_pane(fresh, vsplit=True)

    assert window.active_pane is fresh


def test_a_column_keeps_its_width_when_another_opens():
    """
    The point of the mode. Every other layout would have made this
    column narrower.
    """
    window, _ = a_strip(1)
    first = window.root[0]
    window.column_widths[first] = 2 / 3

    window.add_pane(a_pane(), vsplit=True)

    assert window.column_width(first) == 2 / 3


def test_the_strip_grows_a_column_at_a_time():
    window, _ = a_strip(1)

    for _ in range(3):
        window.add_pane(a_pane(), vsplit=True)

    assert len(window.root) == 4


# ----------------------------------------------------------------------
# A vertical split stacks inside the column.


def test_a_vertical_split_stacks_inside_the_column():
    window, _ = a_strip(2)

    window.add_pane(a_pane(), vsplit=False)

    assert shape(window.root) == ["V", ["H", "p"], ["H", "p", "p"]]


def test_a_stack_does_not_add_a_column():
    window, _ = a_strip(2)

    window.add_pane(a_pane(), vsplit=False)

    assert len(window.root) == 2


def test_a_stacked_pane_keeps_the_width_of_the_column_it_joined():
    """
    A column that became a stack should not change width under a
    person. The width belonged to the pane, and it has to move to the
    split that took the pane's place.
    """
    window, _ = a_strip(2)
    column = window._column_of(window.active_pane)
    window.column_widths[column] = 1 / 3

    window.add_pane(a_pane(), vsplit=False)

    assert window.column_width(window._column_of(window.active_pane)) == 1 / 3


def test_a_column_nobody_resized_is_still_half():
    window, _ = a_strip(2)

    window.add_pane(a_pane(), vsplit=False)

    for column in window.root:
        assert window.column_width(column) == DEFAULT_COLUMN_WIDTH


# ----------------------------------------------------------------------
# Which column a pane is in.


def test_a_pane_in_the_root_is_its_own_column():
    "A column opened by a horizontal split is a bare pane."
    window, _ = a_strip(2)
    alone = window.active_pane

    assert alone in list(window.root)
    assert window._column_of(alone) is alone


def test_a_pane_in_a_stack_is_in_the_stack_s_column():
    window, _ = a_strip(2)
    window.add_pane(a_pane(), vsplit=False)
    deep = window.active_pane

    column = window._column_of(deep)

    assert column in list(window.root)
    assert deep not in list(window.root)


# ----------------------------------------------------------------------
# Outside a strip, nothing changed.


def test_a_window_that_is_not_a_strip_splits_the_pane():
    "The old behaviour, on the same calls."
    window = Window()
    window.add_pane(a_pane())

    window.add_pane(a_pane(), vsplit=True)

    assert shape(window.root) == ["H", ["V", "p", "p"]]


def test_a_window_that_is_not_a_strip_stacks_the_old_way():
    window = Window()
    window.add_pane(a_pane())

    window.add_pane(a_pane(), vsplit=False)

    assert shape(window.root) == ["H", "p", "p"]
