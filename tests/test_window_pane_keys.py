"""
What a key press in a window pane becomes: an evdev key, and the
modifiers held around it. Lillecarl/pymux#620.
"""

from __future__ import annotations

from prompt_toolkit.keys import Keys

from pymux.window_pane import evdev_of

SHIFT, CTRL = 42, 29


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
