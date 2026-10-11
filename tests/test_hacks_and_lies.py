"""
What a pane answers XTVERSION with: pymux's own name, and a lie to a
program that picks its path by a terminal's brand. Lillecarl/pymux#593.
"""

from __future__ import annotations

import pytest

from pymux import hacks_and_lies
from pymux.hacks_and_lies import PYMUX_VERSION, version
from pymux.main import Pymux


def test_a_pane_names_pymux():
    assert version("vim", "xterm-kitty") == PYMUX_VERSION
    assert PYMUX_VERSION.startswith("pymux(")


def test_yazi_in_kitty_hears_kitty():
    "yazi gives Kgp to the kitty brand and Sixel first to one it does not know."
    assert "kitty" in version("yazi", "xterm-kitty")
    # A process started by its path names itself by that path.
    assert "kitty" in version("/nix/store/x-yazi/bin/yazi", "xterm-kitty")


def test_yazi_elsewhere_hears_pymux():
    assert version("yazi", "foot") == PYMUX_VERSION
    assert version("yazi", None) == PYMUX_VERSION


def test_an_integrated_server_reads_its_own_term(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-kitty")
    assert hacks_and_lies.outer_term(None) == "xterm-kitty"


@pytest.fixture
async def pymux():
    mux = Pymux()
    async with mux.running():
        await mux.create_window("sleep 30")
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


@pytest.mark.parametrize("program", ["/nix/store/x-yazi/bin/yazi", "vim"])
async def test_a_pane_asks_the_rules_at_each_query(pymux, monkeypatch, program):
    monkeypatch.setenv("TERM", "xterm-kitty")
    pane = pymux.arrangement.get_active_window().active_pane
    monkeypatch.setattr(pane.process, "get_name", lambda: program)
    assert pane.screen.version() == version(program, "xterm-kitty")
