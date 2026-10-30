"""
What a key press in a window pane becomes: an evdev key, and the
modifiers held around it. Lillecarl/pymux#620.
"""

from __future__ import annotations

from prompt_toolkit.keys import Keys
from pyte.keys import parse_key_data

from pymux.wayland.window_pane import WindowWidget, evdev_of, evdev_of_event

SHIFT, CTRL, ALT = 42, 29, 56


def test_a_letter_is_its_key():
    assert evdev_of("a") == ((), 30)
    assert evdev_of("z") == ((), 44)


def test_a_capital_is_shift_and_the_letter():
    assert evdev_of("A") == ((SHIFT,), 30)


def test_a_shifted_symbol_is_shift_and_its_key():
    assert evdev_of("!") == ((SHIFT,), 2)
    assert evdev_of("1") == ((), 2)
    assert evdev_of("?") == ((SHIFT,), 53)


def test_named_keys():
    assert evdev_of(Keys.Enter) == ((), 28)
    assert evdev_of(Keys.Backspace) == ((), 14)
    assert evdev_of(Keys.Tab) == ((), 15)
    assert evdev_of(Keys.Up) == ((), 103)
    assert evdev_of(Keys.F12) == ((), 88)


def test_control_and_a_letter():
    assert evdev_of(Keys.ControlC) == ((CTRL,), 46)


def test_a_key_no_keyboard_types_is_none():
    assert evdev_of("é") is None


def _event(data: str):
    [event] = parse_key_data(data)
    return evdev_of_event(event)


def test_control_and_a_character_with_no_control_code():
    # foot's zoom, as kitty and as xterm's modifyOtherKeys write it.
    # Lillecarl/pymux#627.
    assert _event("\x1b[61;5u") == ((CTRL,), 13)
    assert _event("\x1b[27;5;61~") == ((CTRL,), 13)
    assert _event("\x1b[45;5u") == ((CTRL,), 12)
    assert _event("\x1b[48;5u") == ((CTRL,), 11)


def test_every_modifier_of_the_event_is_held():
    assert _event("\x1b[61;6u") == ((SHIFT, CTRL), 13)
    assert _event("\x1b[61;7u") == ((CTRL, ALT), 13)


def test_a_release_or_a_key_no_keyboard_types_presses_nothing():
    assert _event("\x1b[61;5:3u") is None
    assert _event("\x1b[57376u") is None


class _Wayland:
    def __init__(self) -> None:
        self.pressed: list[tuple[tuple[int, ...], int]] = []

    def press(self, _toplevel, modifiers, code, _time) -> None:
        self.pressed.append((modifiers, code))


def test_a_window_presses_the_keys_of_a_sequence():
    wayland = _Wayland()
    widget = WindowWidget(wayland, 1, lambda: True)
    widget.press_data("\x1b[61;5u\x1b[61;5:3u\x1b[45;5u")
    assert wayland.pressed == [((CTRL,), 13), ((CTRL,), 12)]
