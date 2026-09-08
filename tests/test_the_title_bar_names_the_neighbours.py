"""
Where the three parts of a title bar land, in cells.

`test_the_title_bar_lays_out.py` judges the arithmetic on its own.
This renders pymux's real layout and reads the row the bars are drawn
on, because that is the only place two things exist: which pane each
bar belongs to, and which pane is beside it.

Every pane is given a name, so a bar says out loud which panes it is
naming. Lillecarl/pymux#207.
"""

from test_strip_draws import CHROME, a_client

from pymux.layout import LEFT_MARK, RIGHT_MARK

NAMES = ["alpha", "beta", "gamma"]

#: Wide enough that three panes side by side each have a bar with room
#: for a neighbour on either side. A quarter of a narrow bar holds
#: nothing, which `test_the_title_bar_lays_out.py` asks about instead.
COLUMNS = 120


def a_row_of_named_panes(pymux, names=NAMES):
    "One pane for each name, side by side, named in order."
    window = pymux.arrangement.get_active_window()
    panes = [window.active_pane]

    for _ in names[1:]:
        pymux.handle_command("split-window -h")
        panes.append(window.active_pane)

    for pane, name in zip(panes, names):
        pane.chosen_name = name

    return panes


def bars_of(pymux, draw, panes):
    """
    The title bar of each pane, as a string.

    A bar is a float over its own pane, so where the pane was drawn is
    where its bar is. Reading a fixed slice of the row instead would
    depend on how the window was divided.
    """
    rows = draw()
    drawn_at = pymux.get_client_state().layout_manager.pane_write_positions

    return [
        rows[0][drawn_at[pane].xpos : drawn_at[pane].xpos + drawn_at[pane].width]
        for pane in panes
    ]


def middle_of(bar, name):
    "How far the centre of that name is from the centre of the bar."
    return abs((bar.index(name) + len(name) / 2) - len(bar) / 2)


# ----------------------------------------------------------------------
# The three parts.


def test_a_pane_names_the_pane_on_each_side():
    with a_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_named_panes(pymux)
        bar = bars_of(pymux, draw, panes)[1]

        # The left edge: the pane's own number, then a mark pointing
        # that way and the name of the pane on the left. The numbers
        # start at zero, as tmux's do.
        assert bar.split()[:3] == ["1", LEFT_MARK, "alpha"], repr(bar)

        # The right edge, and the pane's own title between them.
        assert bar.rstrip().endswith("gamma " + RIGHT_MARK), repr(bar)
        assert "beta" in bar, repr(bar)


def test_the_pane_at_the_left_end_names_nothing_on_its_left():
    with a_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_named_panes(pymux)
        bar = bars_of(pymux, draw, panes)[0]

        assert "alpha" in bar, repr(bar)
        assert LEFT_MARK not in bar, repr(bar)
        assert bar.rstrip().endswith("beta " + RIGHT_MARK), repr(bar)


def test_the_pane_at_the_right_end_names_nothing_on_its_right():
    with a_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_named_panes(pymux)
        bar = bars_of(pymux, draw, panes)[2]

        assert bar.split()[:3] == ["2", LEFT_MARK, "beta"], repr(bar)
        assert RIGHT_MARK not in bar, repr(bar)
        assert bar.rstrip().endswith("gamma"), repr(bar)


def test_a_lone_pane_names_neither_side():
    with a_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_named_panes(pymux, ["alone"])
        bar = bars_of(pymux, draw, panes)[0]

        assert middle_of(bar, "alone") <= 1, repr(bar)


# ----------------------------------------------------------------------
# The middle stays in the middle.


def test_the_pane_s_own_name_is_in_the_middle_of_its_bar():
    """
    Centred over the pane, and not over what the neighbours left of
    it: a name growing on one side may not slide the title sideways.
    """
    with a_client(CHROME, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_named_panes(pymux)
        bars = bars_of(pymux, draw, panes)

        for bar, name in zip(bars, NAMES):
            assert middle_of(bar, name) <= 1, (name, repr(bar))


def test_a_strip_names_a_column_that_is_off_the_screen():
    """
    The reason the bar carries the names at all. A strip runs past the
    edge of the screen, and a name is how a person knows what is out
    there. Two cells of the next column used to be the answer, which
    said that something was there and nothing about what.
    Lillecarl/pymux#198, Lillecarl/pymux#209.

    Two columns of half a window fit exactly, so the column on one
    side of the middle one is always on the screen. The one asked
    about here is the other.
    """
    with a_client(CHROME + ["set-window-option strip on"], columns=COLUMNS) as (
        pymux,
        draw,
    ):
        panes = a_row_of_named_panes(pymux)
        state = pymux.get_client_state()

        # Onto the middle column, the way a key does it.
        pymux.handle_command("select-pane -L")
        state.sync_focus()
        bar = bars_of(pymux, draw, panes)[1]

        assert "alpha" in bar, repr(bar)
        assert "gamma" in bar, repr(bar)

        # And the one it names on the left is off the screen.
        drawn_at = state.layout_manager.pane_write_positions
        assert drawn_at[panes[0]].xpos < 0, drawn_at[panes[0]]
