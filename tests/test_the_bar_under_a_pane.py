"""
The bar under a pane, which names what is above it and what is below.

`test_the_title_bar_names_the_neighbours.py` is the same reading for
the bar over a pane. This one renders pymux's real layout as well,
because three things have to agree or the rows drift: the padding
between stacked panes, the row kept under the whole layout, and the
float that draws the bar. Lillecarl/pymux#211.
"""

from test_strip_draws import CHROME, ROWS, create_client

from pymux.layout import ABOVE_MARK, BELOW_MARK

NAMES = ["top", "middle", "bottom"]

COLUMNS = 60


def a_stack_of_named_panes(pymux, names=NAMES):
    "One pane for each name, stacked, named from the top down."
    window = pymux.arrangement.get_active_window()
    panes = [window.active_pane]

    for _ in names[1:]:
        pymux.handle_command("split-window -v")
        panes.append(window.active_pane)

    for pane, name in zip(panes, names):
        pane.chosen_name = name

    return panes


def bars_under(pymux, draw, panes):
    """
    The bar under each pane, as a string.

    A bar is a float one row below its own pane, so where the pane was
    drawn says which row to read.
    """
    rows = draw()
    drawn_at = pymux.get_client_state().layout_manager.pane_write_positions

    return [
        rows[drawn_at[pane].ypos + drawn_at[pane].height][
            drawn_at[pane].xpos : drawn_at[pane].xpos + drawn_at[pane].width
        ]
        for pane in panes
    ]


# ----------------------------------------------------------------------
# What the bar says.


def test_a_pane_in_the_middle_names_the_one_above_and_the_one_below():
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux)
        bar = bars_under(pymux, draw, panes)[1]

        assert bar.split() == [ABOVE_MARK, "top", BELOW_MARK, "bottom"], repr(bar)


def test_the_top_of_a_stack_names_only_what_is_below():
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux)
        bar = bars_under(pymux, draw, panes)[0]

        assert bar.split() == [BELOW_MARK, "middle"], repr(bar)
        assert ABOVE_MARK not in bar, repr(bar)


def test_the_bottom_of_a_stack_names_only_what_is_above():
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux)
        bar = bars_under(pymux, draw, panes)[2]

        assert bar.split() == [ABOVE_MARK, "middle"], repr(bar)
        assert BELOW_MARK not in bar, repr(bar)


def test_the_names_sit_in_the_middle_of_the_bar():
    "Two marks and one gap, centred as one thing."
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux)
        bar = bars_under(pymux, draw, panes)[1]

        left = len(bar) - len(bar.lstrip())
        right = len(bar) - len(bar.rstrip())

        assert abs(left - right) <= 1, repr(bar)


# ----------------------------------------------------------------------
# The row it costs, and when it costs nothing.


def test_a_window_with_no_stack_keeps_no_row_under_its_panes():
    """
    A single pane and a plain row of panes look exactly as they did.
    There is nothing above or below them to name, so the bar would be
    empty and the row would be spent on nothing.

    The last row of the screen is pymux's own status bar, so a pane
    that gives up nothing ends on the row before it.
    """
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        pymux.handle_command("split-window -h")
        pane = pymux.arrangement.get_active_window().active_pane
        draw()
        drawn_at = pymux.get_client_state().layout_manager.pane_write_positions

        where = drawn_at[pane]

        assert where.ypos + where.height == ROWS - 1, where


def test_a_stack_keeps_two_rows_between_its_panes():
    """
    One for the lower pane's own title bar, and one for the upper
    pane's bar below. They cannot share: the title bar names the pane
    it belongs to, and the bar below names two others.
    """
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux, NAMES[:2])
        draw()
        drawn_at = pymux.get_client_state().layout_manager.pane_write_positions

        top, bottom = (drawn_at[pane] for pane in panes)

        assert bottom.ypos == top.ypos + top.height + 2, (top, bottom)


def test_a_pane_with_nothing_above_or_below_it_draws_no_bar():
    """
    A window can hold a stack in one column and a lone pane beside it.
    The lone pane has nothing to name, and an empty bar is not nothing:
    it fills its row with the title bar style, which drew a coloured
    band under a pane that is not stacked at all.

    Carl, on a picture of it: "we only want that bar if the rectangle
    is split". Lillecarl/pymux#219.

    The row is still there, because the window keeps one for the stack
    beside it. What is in it under this pane is the background.
    """
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        window = pymux.arrangement.get_active_window()
        alone = window.active_pane
        alone.chosen_name = "alone"

        # A column of its own beside it, split into a stack, so the
        # window keeps the row and this pane is not in the stack.
        pymux.handle_command("split-window -h")
        pymux.handle_command("split-window -v")

        (bar,) = bars_under(pymux, draw, [alone])

        assert not bar.strip(), repr(bar)


def test_the_bottom_pane_of_a_stack_has_a_row_under_it():
    """
    The row is kept under the whole layout, not only between panes.

    So the bottom pane ends one row higher than a pane in a window
    with no stack: its own bar, and then the status bar.
    """
    with create_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_stack_of_named_panes(pymux, NAMES[:2])
        draw()
        drawn_at = pymux.get_client_state().layout_manager.pane_write_positions

        where = drawn_at[panes[1]]

        assert where.ypos + where.height == ROWS - 2, where
