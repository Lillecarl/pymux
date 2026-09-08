"""
Three parts on one row: the arithmetic of it.

`lay_out_the_bar` is handed the real width, which is the whole reason
the bar is a control and not three windows. These tests read the row it
returns as one string, because that is what a person sees.

The picture check is what says whether it reads as one bar. This says
whether the cells are where they were meant to be.
Lillecarl/pymux#207.
"""

import pytest
from prompt_toolkit.formatted_text.utils import fragment_list_to_text

from pymux.titlebar import ELLIPSIS, lay_out_the_bar

WIDTH = 40


def bar(number="", left="", middle="", right="", width=WIDTH):
    """
    The row, as a string, from plain names.

    The number is empty unless a test is about it, so that the other
    tests read as the arithmetic they are about.
    """
    return fragment_list_to_text(
        lay_out_the_bar(
            *([("", part)] if part else [] for part in (number, left, middle, right)),
            width,
        )
    )


# ----------------------------------------------------------------------
# Where each part lands.


def test_the_middle_is_centred_over_the_whole_bar():
    row = bar(middle="title")

    assert row == "title".center(WIDTH), repr(row)


def test_the_edges_are_at_the_edges():
    row = bar(left="vim", middle="bash", right="less")

    assert row.startswith("vim"), repr(row)
    assert row.endswith("less"), repr(row)


def test_a_neighbour_does_not_move_the_title():
    """
    The middle is centred over the bar and not over what is left of it,
    so a name growing on one side does not slide the title sideways.
    """
    alone = bar(middle="bash")
    beside = bar(left="vim", middle="bash", right="less")

    assert alone.index("bash") == beside.index("bash"), (alone, beside)


def test_the_row_is_the_width_it_was_given():
    assert len(bar(left="vim", middle="bash", right="less")) == WIDTH


@pytest.mark.parametrize("width", [1, 2, 5, 8, 13, 21, 80, 200])
def test_the_row_is_that_width_whatever_it_holds(width):
    row = bar(left="a long name", middle="a title", right="another name", width=width)

    assert len(row) == width, repr(row)


def test_no_width_at_all_draws_nothing():
    assert bar(left="vim", middle="bash", right="less", width=0) == ""


# ----------------------------------------------------------------------
# What gives way, and in which order.


def test_a_long_neighbour_is_cut_to_a_quarter_of_the_bar():
    row = bar(left="a-very-long-program-name", middle="bash", width=40)

    assert row.startswith("a-very-lo" + ELLIPSIS), repr(row)
    # A quarter of forty, and no more.
    assert len(row) - len(row.lstrip("a-very-long" + ELLIPSIS)) <= 10, repr(row)


def test_a_narrow_bar_keeps_its_own_title_and_drops_the_neighbours():
    """
    Under a handful of cells an edge holds a letter and an ellipsis,
    which names nothing. The pane's own title is what a person needs.
    """
    row = bar(left="vim", middle="bash", right="less", width=12)

    assert "vim" not in row, repr(row)
    assert "less" not in row, repr(row)
    assert "bash" in row, repr(row)


def test_the_middle_gives_way_to_nothing_but_the_edges():
    "It is cut only when the edges have already taken their share."
    row = bar(left="vim", middle="t" * (WIDTH + 10), right="less")

    assert row.startswith("vim"), repr(row)
    assert row.endswith("less"), repr(row)
    assert row.count(ELLIPSIS) == 1, repr(row)


def test_a_middle_that_would_reach_an_edge_is_pushed_off_it():
    """
    Centring is where it starts, and not where it ends: a title as
    wide as the room between the names cannot also sit in the middle
    of the bar, and the names win.

    Thirty-three cells is exactly what "vim" and "less" leave of forty.
    """
    row = bar(left="vim", middle="t" * 33, right="less")

    assert row == "vim" + "t" * 33 + "less", repr(row)


# ----------------------------------------------------------------------
# The pane's own number, which is not an edge.

NUMBER = "  1 "


def test_the_number_is_at_the_far_left_and_is_never_cut():
    row = bar(number=NUMBER, left="a-name-far-too-long", middle="bash", width=WIDTH)

    assert row.startswith(NUMBER), repr(row)


def test_the_number_does_not_take_from_a_neighbour_s_share():
    """
    A quarter of what is left of the bar, and not a quarter of the bar.

    Four cells of a thirty-nine cell bar is nearly half of one edge's
    share. Counting the number against the left edge left `alp…` there
    while `gamma` fitted whole on the right.
    """
    row = bar(number=NUMBER, left=" alpha ", middle=" beta ", right=" gamma ", width=39)

    assert "alpha" in row, repr(row)
    assert "gamma" in row, repr(row)


def test_the_number_does_not_move_the_title():
    "It is centred over the pane, and the number is over the pane too."
    assert bar(middle="title").index("title") == bar(
        number=NUMBER, middle="title"
    ).index("title")


def test_one_neighbour_and_no_other():
    "The left edge is empty when there is no pane on the left."
    row = bar(middle="bash", right="less")

    assert row.startswith(" "), repr(row)
    assert row.endswith("less"), repr(row)
    assert row.index("bash") == (WIDTH - len("bash")) // 2, repr(row)
