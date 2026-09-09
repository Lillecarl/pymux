"""
Choosing how wide a column of a strip is.

`switch-column-width` gives the column the next preset width: a third,
a half, two thirds, and the whole window. The first three are niri's
own, and so is cycling rather than resizing by a step. A person picks
between a few widths that fit together instead of nudging a border
until it looks right, which is what `resize-pane` still does for a
divided layout.

Lillecarl/pymux#198, and the fourth width is Lillecarl/pymux#215.
"""

import pytest

from pymux.arrangement import (
    DEFAULT_COLUMN_WIDTH,
    PRESET_COLUMN_WIDTHS,
    Pane,
    Window,
)


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def a_pane():
    return Pane(terminal=_Fake())


def a_strip(columns=1):
    "A strip with this many columns, active on the last."
    window = Window()
    window.add_pane(a_pane())
    window.strip = True
    for _ in range(columns - 1):
        window.add_pane(a_pane(), vsplit=True)
    return window


def width_now(window):
    "The width of the column the active pane is in."
    return window.column_width(window._column_of(window.active_pane))


def test_the_presets_are_the_ones_niri_ships_and_a_full_one():
    assert PRESET_COLUMN_WIDTHS == (1 / 3, 1 / 2, 2 / 3, 1.0)
    assert DEFAULT_COLUMN_WIDTH == 1 / 2


def test_a_column_can_be_the_whole_window_without_leaving_the_strip():
    """
    Carl asked how a person makes a column full width in a strip, and
    the answer was that they could not: the cycle stopped at two
    thirds, and `resize-pane -Z` leaves the row rather than widening a
    column of it. Lillecarl/pymux#215.
    """
    window = a_strip(2)

    window.switch_column_width(window.active_pane)
    window.switch_column_width(window.active_pane)

    assert width_now(window) == 1.0
    # And the other column is still there, one scroll away.
    assert len(window.root) == 2


def test_the_next_width_after_the_default_is_two_thirds():
    "The default is the middle preset, so forward is the wide one."
    window = a_strip(2)

    window.switch_column_width(window.active_pane)

    assert width_now(window) == 2 / 3


def test_the_previous_width_from_the_default_is_a_third():
    window = a_strip(2)

    window.switch_column_width(window.active_pane, back=True)

    assert width_now(window) == 1 / 3


def test_the_widths_come_round_again():
    window = a_strip(2)
    seen = []

    for _ in range(len(PRESET_COLUMN_WIDTHS) + 1):
        window.switch_column_width(window.active_pane)
        seen.append(width_now(window))

    assert seen[-1] == seen[0]


def test_going_back_undoes_going_forward():
    window = a_strip(2)

    window.switch_column_width(window.active_pane)
    window.switch_column_width(window.active_pane, back=True)

    assert width_now(window) == DEFAULT_COLUMN_WIDTH


@pytest.mark.parametrize("back", [False, True])
def test_a_width_that_is_not_a_preset_steps_onto_the_list(back):
    """
    A column set to something of its own, which a fixed width would
    give. It joins the cycle rather than being stuck outside it.
    """
    window = a_strip(2)
    window.column_widths[window._column_of(window.active_pane)] = 0.42

    window.switch_column_width(window.active_pane, back=back)

    assert width_now(window) in PRESET_COLUMN_WIDTHS


def test_only_the_column_a_person_is_on_changes():
    window = a_strip(3)
    others = [
        window.column_width(column)
        for column in window.root
        if column is not window._column_of(window.active_pane)
    ]

    window.switch_column_width(window.active_pane)

    assert [
        window.column_width(column)
        for column in window.root
        if column is not window._column_of(window.active_pane)
    ] == others


def test_a_pane_in_a_stack_changes_the_whole_column():
    "A stack is one column, and a column has one width."
    window = a_strip(2)
    window.add_pane(a_pane(), vsplit=False)
    column = window._column_of(window.active_pane)

    window.switch_column_width(window.active_pane)

    assert window.column_width(column) == 2 / 3
    assert len(column) == 2
