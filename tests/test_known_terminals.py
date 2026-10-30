"""
What pymux knows about a terminal by its name. Lillecarl/pymux#827.

The XTVERSION answers are the ones real terminals gave pymux.
"""

from __future__ import annotations

import pytest
from pyte.osc import Osc

from pymux.known_terminals import identify, wcwidth_name


@pytest.mark.parametrize(
    ("version", "term", "version_read"),
    [
        ("kitty(0.39.1)", "", (0, 39, 1)),
        ("ghostty 1.3.1-dev+abc", "", (1, 3, 1)),
        ("WezTerm 20240203-110809-5046fc22", "", (20240203,)),
        ("iTerm2 3.6.4", "", (3, 6, 4)),
        ("XTerm(390)", "", (390,)),
        ("", "xterm-kitty", None),
    ],
)
def test_the_version_is_read_from_the_answer(version, term, version_read):
    assert identify(version, term).version == version_read


@pytest.mark.parametrize(
    ("version", "term", "draws"),
    [
        ("kitty(0.39.1)", "xterm-256color", True),
        ("kitty(0.28.0)", "", True),
        ("kitty(0.27.1)", "xterm-kitty", False),
        ("ghostty 1.3.1", "xterm-ghostty", True),
        ("ghostty 1.3.1-dev+abc", "", True),
        # WezTerm answers the graphics query and draws no placeholder.
        ("WezTerm 20240203", "xterm-kitty", False),
        # TERM gives no version, and placeholders start at one.
        ("", "xterm-kitty", False),
    ],
)
def test_the_terminals_that_draw_placeholders(version, term, draws):
    assert identify(version, term).draws_placeholders() is draws


@pytest.mark.parametrize(
    ("version", "draws"),
    [
        ("kitty(0.44.0)", True),
        ("kitty(0.43.1)", False),
        ("ghostty 1.2.3", True),
        ("iTerm2 3.6.1", True),
        ("WezTerm 20240203", False),
        ("", False),
    ],
)
def test_the_terminals_that_draw_a_progress(version, draws):
    assert identify(version, "").draws_progress() is draws


@pytest.mark.parametrize(
    ("version", "term", "shown"),
    [
        ("iTerm2 3.6.4", "", Osc.MESSAGE),
        ("WezTerm 20240203-110809-5046fc22", "", Osc.SIMPLE_NOTIFICATION),
        ("ghostty 1.2.3", "", Osc.SIMPLE_NOTIFICATION),
        ("foot(1.28.0)", "", Osc.SIMPLE_NOTIFICATION),
        ("", "xterm-ghostty", Osc.SIMPLE_NOTIFICATION),
        ("XTerm(397)", "", None),
        ("", "", None),
    ],
)
def test_the_notification_a_terminal_shows_without_the_query(version, term, shown):
    assert identify(version, term).notification_osc == shown


@pytest.mark.parametrize(
    ("version", "saves"),
    [("iTerm2 3.5.14", True), ("kitty(0.49.2)", False), ("ghostty 1.3.1", False), ("", False)],
)
def test_only_iterm2_saves_a_download(version, saves):
    assert identify(version, "").saves_downloads is saves


@pytest.mark.parametrize(("version", "first"), [("kitty(0.49.2)", 0), ("ghostty 1.3.1", 1), ("", 1)])
def test_kitty_numbers_pixels_from_zero(version, first):
    assert identify(version, "").first_pixel == first


@pytest.mark.parametrize(
    ("version", "term", "name"),
    [
        ("kitty(0.49.2)", "xterm-256color", "kitty"),
        ("ghostty 1.2.0", "", "ghostty"),
        ("WezTerm 20240203-110809-5046fc22", "", "wezterm"),
        ("XTerm(390)", "xterm", "xterm"),
        ("", "xterm-kitty", "kitty"),
        ("", "wezterm", "wezterm"),
        ("", "xterm-256color", None),
        ("foot(1.20.2)", "foot", None),
    ],
)
def test_the_key_profile_is_named_by_the_answer_and_then_by_term(version, term, name):
    assert identify(version, term).key_profile == name


@pytest.mark.parametrize(
    ("version", "term", "name"),
    [
        ("WezTerm 20240203-110809-5046fc22", "xterm-256color", "wezterm"),
        ("iTerm2 3.7.6", "xterm-256color", "iterm2"),
        ("ghostty 1.4.0", "xterm-ghostty", "ghostty"),
        ("kitty(0.39.1)", "xterm-kitty", "kitty"),
        ("", "xterm-kitty", "xterm-kitty"),
        ("", "xterm-256color", None),
        ("SomethingNew 1.0", "screen", None),
    ],
)
def test_the_wcwidth_table_is_named_by_the_answer_and_then_by_term(version, term, name):
    assert wcwidth_name(version, term) == name
