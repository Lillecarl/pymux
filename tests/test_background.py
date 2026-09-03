"""
The background of dots must keep its pattern.
"""
from prompt_toolkit.layout.screen import Screen, WritePosition

from pymux.layout import Background


def _draw(xpos, ypos, width, height):
    screen = Screen()
    Background().write_to_screen(
        screen,
        None,
        WritePosition(xpos=xpos, ypos=ypos, width=width, height=height),
        "",
        False,
        None,
    )
    return screen


def test_every_third_cell_is_a_dot():
    screen = _draw(0, 0, 9, 4)
    for y in range(4):
        for x in range(9):
            cell = screen.data_buffer[y][x]
            assert cell.char == ("." if (x + y) % 3 == 0 else " ")
            assert cell.style == "class:background"


def test_the_pattern_follows_the_position():
    screen = _draw(5, 7, 6, 5)
    for y in range(7, 12):
        for x in range(5, 11):
            assert screen.data_buffer[y][x].char == ("." if (x + y) % 3 == 0 else " ")


def test_nothing_outside_the_area_is_written():
    screen = _draw(2, 3, 4, 2)
    assert set(screen.data_buffer) == {3, 4}
    assert set(screen.data_buffer[3]) == {2, 3, 4, 5}
