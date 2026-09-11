"""
The order `Window.panes` is in, which is the order a person sees.

`get_pane_index` is `panes.index(pane)`, and that number is what
`select-pane -t <n>` takes, what `display-panes` puts up, and what
every title bar draws at its left edge. So the order is not an
internal detail: it is on the screen all day.

It used to be the order of a walk that took each split's own panes
before the splits inside it. Every pane sitting directly in the root
was numbered before every pane in a nested split, whatever its place
on the screen. A strip meets that at once, because `Window.strip`
wraps the window it turns into a row: the first column is a nested
split, so its panes came last. Lillecarl/pymux#210.
"""

from pymux.arrangement import HSplit, Pane, VSplit, Window


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def create_pane():
    return Pane(terminal=_Fake())


def create_window(root):
    window = Window()
    window.root = root
    return window


def test_a_row_is_numbered_from_the_left():
    panes = [create_pane() for _ in range(3)]
    window = create_window(VSplit(panes))

    assert window.panes == panes


def test_a_stack_is_numbered_from_the_top():
    panes = [create_pane() for _ in range(3)]
    window = create_window(HSplit(panes))

    assert window.panes == panes


def test_a_nested_split_is_numbered_where_it_sits():
    """
    The one that was wrong. `first` is inside a split and `second` is
    not, and `first` is to the left, so it is numbered first.
    """
    first, second = create_pane(), create_pane()
    window = create_window(VSplit([HSplit([first]), second]))

    assert window.panes == [first, second]
    assert window.get_pane_index(first) == 0
    assert window.get_pane_index(second) == 1


def test_a_strip_numbers_its_columns_from_the_left():
    """
    What the picture showed. The first column of a strip is a stack of
    one, because turning the mode on wraps whatever the window was.
    """
    window = Window()
    opened = [create_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(2):
        opened.append(create_pane())
        window.add_pane(opened[-1], vsplit=True)

    assert window.panes == opened
    assert [window.get_pane_index(pane) for pane in opened] == [0, 1, 2]


def test_a_deep_tree_reads_left_to_right_and_top_to_bottom():
    a, b, c, d = (create_pane() for _ in range(4))
    window = create_window(VSplit([HSplit([a, b]), VSplit([c, d])]))

    assert window.panes == [a, b, c, d]
