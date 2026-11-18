"""
`automatic-rename` names a window after the program of its active pane;
off, the window keeps the name it has, and turning it on again takes
the window back from a name a person gave, as in tmux.
Lillecarl/pyterm#1035.

A pane's own `chosen_name` stands in for the program it runs.
"""

from __future__ import annotations

from types import SimpleNamespace

from pymux.core.pane import Pane
from pymux.core.window_tree import Window
from pymux.options.window_options import _name_follows

_PYMUX = SimpleNamespace(invalidate=lambda: None)


def _window_running(program: str) -> tuple[Window, Pane]:
    window = Window()
    pane = Pane(None)  # type: ignore[arg-type]
    pane.chosen_name = program
    window.root.append(pane)
    window.active_pane = pane
    return window, pane


def test_the_name_follows_the_program():
    window, pane = _window_running("fish")
    assert window.name == "fish"
    pane.chosen_name = "vim"
    assert window.name == "vim"


def test_off_the_window_keeps_its_name():
    window, pane = _window_running("fish")
    window.options.automatic_rename = False
    assert window.name == "fish"

    pane.chosen_name = "vim"
    assert window.name == "fish"

    window.options.automatic_rename = True
    _name_follows(_PYMUX, window)  # type: ignore[arg-type]
    assert window.name == "vim"


def test_turning_it_on_drops_a_name_a_person_gave():
    window, _pane = _window_running("fish")
    window.chosen_name = "build"
    window.options.automatic_rename = False
    assert window.name == "build"

    window.options.automatic_rename = True
    _name_follows(_PYMUX, window)  # type: ignore[arg-type]
    assert window.name == "fish"
