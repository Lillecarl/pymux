"""
`pane-browser`: which programs in a pane find `pymux-browser` as their
browser. Lillecarl/pymux#653.
"""

from __future__ import annotations

import pytest

from pymux import main
from pymux.enums import PaneBrowser
from pymux.main import Pymux

SHIM = "/nix/store/x-pymux/bin/pymux-browser"


@pytest.fixture
def pymux(monkeypatch):
    monkeypatch.setattr(main, "_browser_shim", lambda: SHIM)
    return Pymux()


def environment_of(pymux) -> dict[str, str]:
    environment = {"PATH": "/usr/bin"}
    pymux._shim_pane_environment(environment, pymux.current_session.options)
    return environment


def test_agents_get_the_browser_of_the_person_by_default(pymux):
    "An agent's browser is one a person wants to see."
    assert pymux.session_defaults.pane_browser == PaneBrowser.AGENTS
    environment = environment_of(pymux)
    assert environment["PLAYWRIGHT_MCP_EXECUTABLE_PATH"] == SHIM
    assert "PUPPETEER_EXECUTABLE_PATH" not in environment


def test_all_names_it_for_puppeteer_too(pymux):
    pymux.handle_command("set-option pane-browser all")
    environment = environment_of(pymux)
    assert environment["PLAYWRIGHT_MCP_EXECUTABLE_PATH"] == environment["PUPPETEER_EXECUTABLE_PATH"] == SHIM


def test_off_names_it_for_nobody(pymux):
    pymux.handle_command("set-option pane-browser off")
    environment = environment_of(pymux)
    assert "PLAYWRIGHT_MCP_EXECUTABLE_PATH" not in environment
    assert "PUPPETEER_EXECUTABLE_PATH" not in environment
