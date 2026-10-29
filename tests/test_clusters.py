"""
A grapheme cluster fitted to the outer terminal. Lillecarl/pymux#798.

The XTVERSION answers are the ones four real terminals gave pymux.
"""

from __future__ import annotations

import pytest

from pymux.clusters import fitted, term_program_of

FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"
FLAG = "\U0001f1f8\U0001f1ea"
THUMB = "\U0001f44d\U0001f3fd"


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
def test_the_terminal_is_named_by_its_version_then_by_term(version, term, name):
    assert term_program_of(version, term) == name


def test_a_terminal_that_draws_a_cluster_two_wide_gets_it_as_it_is():
    frame = "\x1b[1;1H" + FAMILY + FLAG + THUMB + "x"
    assert fitted(frame, "kitty") == frame


def test_a_cluster_drawn_narrower_is_padded_to_its_cell():
    "WezTerm draws a flag one column wide."
    assert fitted("a" + FLAG + "b", "wezterm") == "a" + FLAG + " b"


def test_a_cluster_drawn_wider_goes_out_as_its_first_character():
    "xterm draws a family as its three faces."
    assert fitted(FAMILY + "b", "xterm") == "\U0001f468b"


def test_a_title_keeps_its_cluster():
    title = "\x1b]0;" + FLAG + "\x07"
    assert fitted(title + FLAG, "wezterm") == title + FLAG + " "


def test_a_frame_without_a_joiner_is_not_read():
    frame = "中❤️"
    assert fitted(frame, "xterm") is frame
