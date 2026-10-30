"Box drawing and block elements, drawn from the cell as foot draws them. Lillecarl/pymux#778."

from __future__ import annotations

import pytest

from pymux.layout.box_drawing import INK, arms, light_thickness, mask

WIDTH, HEIGHT = 9, 18


def _ink(code: int, box: tuple[int, int, int, int]) -> set[int]:
    "Every alpha inside `box` of one cell."
    found = mask(code, WIDTH, HEIGHT)
    assert found is not None
    return set(found.crop(box).get_flattened_data())


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("┌", {"down": "light", "right": "light"}),
        ("┦", {"up": "heavy", "left": "light", "down": "light"}),
        ("╪", {"up": "light", "down": "light", "left": "double", "right": "double"}),
        ("╼", {"left": "light", "right": "heavy"}),
    ],
)
def test_the_arms_are_read_from_the_name(char, expected):
    assert arms(ord(char)) == expected


def test_every_line_has_an_arm_and_nothing_else_is_read_as_one():
    for code in range(0x2500, 0x2580):
        found = arms(code)
        assert found is None or found, hex(code)
    assert [arms(ord(char)) for char in "┄╭╱▀"] == [None, None, None, None]


def test_a_character_outside_the_range_is_the_fonts():
    assert mask(ord("a"), WIDTH, HEIGHT) is None


def test_a_line_reaches_both_edges_of_its_cell():
    "So a row of ─ or a column of │ has no gap between cells."
    light = light_thickness(WIDTH, HEIGHT)
    top, left = (HEIGHT - light) // 2, (WIDTH - light) // 2
    assert _ink(ord("─"), (0, top, WIDTH, top + light)) == {INK}
    assert _ink(ord("│"), (left, 0, left + light, HEIGHT)) == {INK}
    assert _ink(ord("─"), (0, 0, WIDTH, top)) == {0}


def test_a_double_line_is_two_lines():
    light = light_thickness(WIDTH, HEIGHT)
    top = (HEIGHT - 3 * light) // 2
    rows = [_ink(ord("═"), (0, y, WIDTH, y + 1)) for y in range(top, top + 3 * light)]
    assert rows == [{INK}] * light + [{0}] * light + [{INK}] * light


def test_a_single_line_stops_at_a_double_that_goes_through_and_crosses_one_it_goes_through():
    light = light_thickness(WIDTH, HEIGHT)
    gap = (WIDTH - 3 * light) // 2 + light
    row = (HEIGHT - light) // 2
    assert _ink(ord("╟"), (gap, row, gap + light, row + light)) == {0}
    column = (WIDTH - light) // 2
    top = (HEIGHT - 3 * light) // 2 + light
    assert _ink(ord("╪"), (column, top, column + light, top + light)) == {INK}


def test_a_rounded_corner_reaches_the_edges_it_turns_between():
    light = light_thickness(WIDTH, HEIGHT)
    column, row = (WIDTH - light) // 2, (HEIGHT - light) // 2
    assert _ink(ord("╭"), (column, HEIGHT - 1, column + light, HEIGHT)) == {INK}
    assert _ink(ord("╭"), (WIDTH - 1, row, WIDTH, row + light)) == {INK}
    assert _ink(ord("╭"), (0, 0, WIDTH, 2)) == {0}


def test_a_shade_takes_a_quarter_a_half_or_three_of_the_ink():
    assert [_ink(ord(char), (0, 0, WIDTH, HEIGHT)) for char in "░▒▓"] == [{0x40}, {0x80}, {0xC0}]
