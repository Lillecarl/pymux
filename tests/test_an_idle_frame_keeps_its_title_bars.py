"""
An idle frame reads its title bars and does not draw them again.

A frame that changes nothing measures a new plan, and every title bar
asked that plan three neighbours and laid itself out again: about a
millisecond of a 5.8 ms frame with eight panes. The plan of the last
frame is kept when nothing moved, a plan keeps what it answered, and a
bar that reads the same writes the cells it wrote before.
Lillecarl/pymux#578.
"""

from __future__ import annotations

from pymux.layout.plane import GROUND, Plan, Slot
from pymux.layout.rects import Rect, Side
from pymux.layout.titlebar import PaneTitleBar


class _Pane:
    "Enough of a pane for a slot to hold."


LEFT, RIGHT = _Pane(), _Pane()


def side_by_side(width: int = 10) -> Plan:
    "Two panes, one beside the other, measured afresh."
    return Plan({GROUND: {Slot(LEFT): Rect(0, 0, width, 5), Slot(RIGHT): Rect(width, 0, width, 5)}})


def test_two_measures_of_one_arrangement_are_the_same_plan():
    assert side_by_side().same_as(side_by_side())


def test_a_resize_is_another_plan():
    assert not side_by_side(10).same_as(side_by_side(12))


def test_a_swap_is_another_plan():
    swapped = Plan({GROUND: {Slot(RIGHT): Rect(0, 0, 10, 5), Slot(LEFT): Rect(10, 0, 10, 5)}})
    assert not side_by_side().same_as(swapped)


def test_a_plan_keeps_what_it_answered():
    plan = side_by_side()
    left = plan.slot_of(LEFT)

    first = plan.neighbour(left, Side.RIGHT)
    plan.layers[GROUND].rects.clear()

    assert plan.neighbour(left, Side.RIGHT) is first
    assert first is not None and first.shown is RIGHT


def bar(title: list) -> PaneTitleBar:
    return PaneTitleBar(
        get_number=lambda: [("", "  1 ")],
        get_left=list,
        get_middle=lambda: list(title),
        get_right=list,
    )


def test_a_bar_that_reads_the_same_has_the_same_version():
    title = [("", " bash ")]
    assert bar(title).content_version(40, 1) == bar(title).content_version(40, 1)


def test_a_new_title_is_a_new_version():
    title = [("", " bash ")]
    one = bar(title)
    before = one.content_version(40, 1)
    title[0] = ("", " vim ")

    assert one.content_version(40, 1) != before


def test_a_new_width_is_a_new_version():
    one = bar([("", " bash ")])
    assert one.content_version(40, 1) != one.content_version(41, 1)
