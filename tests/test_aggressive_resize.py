"""
`aggressive-resize on` is what pymux always does: a window takes the
size of the clients that show it (`Pymux.clients_watching`). So `on`
is taken and `off` says why it is not. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

from run_commands import complaints

from pymux.core.main import Pymux


def test_on_is_taken():
    assert complaints(Pymux(), "setw -g aggressive-resize on") == []


def test_off_says_why_not():
    (complaint,) = complaints(Pymux(), "setw -g aggressive-resize off")
    assert "always sizes a window by the clients that show it" in complaint
