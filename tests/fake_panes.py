"""
Panes with no program behind them, for tests of the arrangement alone.
"""

from __future__ import annotations

from pymux.core.window_tree import Pane


class FakeTerminal:
    "Enough of a terminal for the arrangement to hold a pane."


def create_pane(name: str | None = None) -> Pane:
    "A pane over no terminal, named if a name is given."
    pane = Pane(terminal=FakeTerminal())
    if name is not None:
        pane.chosen_name = name
    return pane
