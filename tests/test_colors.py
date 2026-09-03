"""
Tests for the colour depth detection (`pymux.colors`).
"""
import pytest
from prompt_toolkit.output import ColorDepth

from pymux.colors import (
    TRUECOLOR_PROBE,
    ColorDetection,
    depth_from_environment,
    reports_truecolor,
)


def detection(term="", colorterm="", forced=None):
    result = ColorDetection(forced)
    result.term = term
    result.colorterm = colorterm
    return result


def test_the_probe_sets_a_colour_and_asks_for_it_back():
    assert TRUECOLOR_PROBE.startswith("\x1b[38;2;1;2;3m")
    assert "\x1bP$qm\x1b\\" in TRUECOLOR_PROBE
    assert TRUECOLOR_PROBE.endswith("\x1b[0m")  # The attributes go back.


# ----------------------------------------------------------------------
# Reading the probe reply.


@pytest.mark.parametrize(
    "reply",
    [
        # The semicolon form.
        "\x1bP1$r38;2;1;2;3m\x1b\\",
        # The colon form, with the empty colour space id.
        "\x1bP1$r38:2::1:2:3m\x1b\\",
        # The colon form without the colour space id.
        "\x1bP1$r38:2:1:2:3m\x1b\\",
        # The colon form with a colour space id that is not empty.
        "\x1bP1$r38:2:0:1:2:3m\x1b\\",
        # Other attributes around it.
        "\x1bP1$r0;1;38;2;1;2;3;48;5;16m\x1b\\",
        # Terminals disagree about the validity digit.
        "\x1bP0$r38;2;1;2;3m\x1b\\",
        "\x1bP$r38;2;1;2;3m\x1b\\",
        # The eight bit string terminator.
        "\x1bP1$r38;2;1;2;3m\x9c",
    ],
)
def test_a_reply_that_keeps_the_colour_means_truecolor(reply):
    assert reports_truecolor(reply)


@pytest.mark.parametrize(
    "reply",
    [
        # The terminal reduced the colour to an index.
        "\x1bP1$r38;5;16m\x1b\\",
        # It dropped the colour.
        "\x1bP1$r0m\x1b\\",
        # It kept a colour, but not the one that was asked for.
        "\x1bP1$r38;2;4;5;6m\x1b\\",
        # Not a DECRQSS reply at all.
        "\x1b[?62;1;6c",
        "\x1b_Gi=31;OK\x1b\\",
        "",
        # A reply that never ends.
        "\x1bP1$r38;2;1;2;3m",
    ],
)
def test_other_replies_do_not_mean_truecolor(reply):
    assert not reports_truecolor(reply)


def test_the_probe_reply_raises_the_depth():
    detect = detection(term="xterm-256color")
    assert detect.depth == ColorDepth.DEPTH_8_BIT

    detect.handle_reply("\x1bP1$r38:2::1:2:3m\x1b\\")
    assert detect.truecolor
    assert detect.depth == ColorDepth.DEPTH_24_BIT


def test_an_unrelated_reply_changes_nothing():
    detect = detection(term="xterm-256color")
    detect.handle_reply("\x1b[6;20;10t")
    detect.handle_reply("\x1b_Gi=31;OK\x1b\\")
    assert not detect.truecolor
    assert detect.depth == ColorDepth.DEPTH_8_BIT


# ----------------------------------------------------------------------
# The fallback chain.


@pytest.mark.parametrize(
    "term,colorterm,expected",
    [
        # COLORTERM is the second answer.
        ("xterm-256color", "truecolor", ColorDepth.DEPTH_24_BIT),
        ("xterm-256color", "24bit", ColorDepth.DEPTH_24_BIT),
        ("xterm", "TrueColor", ColorDepth.DEPTH_24_BIT),
        # A COLORTERM that says nothing useful.
        ("xterm-256color", "1", ColorDepth.DEPTH_8_BIT),
        # TERM is the last one.
        ("xterm-direct", "", ColorDepth.DEPTH_24_BIT),
        ("screen-256color", "", ColorDepth.DEPTH_8_BIT),
        ("tmux-256color", "", ColorDepth.DEPTH_8_BIT),
        ("linux", "", ColorDepth.DEPTH_4_BIT),
        ("vt100", "", ColorDepth.DEPTH_4_BIT),
        ("eterm-color", "", ColorDepth.DEPTH_4_BIT),
        ("dumb", "", ColorDepth.DEPTH_1_BIT),
        ("", "", ColorDepth.DEPTH_1_BIT),
        # Anything else is a terminal from this century.
        ("xterm", "", ColorDepth.DEPTH_8_BIT),
        ("foot", "", ColorDepth.DEPTH_8_BIT),
    ],
)
def test_the_environment_decides_without_a_probe(term, colorterm, expected):
    assert depth_from_environment(term, colorterm) == expected
    assert detection(term=term, colorterm=colorterm).depth == expected


def test_the_term_name_is_read_case_insensitively():
    assert depth_from_environment("XTERM-256COLOR", "") == ColorDepth.DEPTH_8_BIT


# ----------------------------------------------------------------------
# The command line wins.


def test_a_forced_depth_beats_the_environment():
    detect = detection(term="linux", forced=ColorDepth.DEPTH_24_BIT)
    assert detect.depth == ColorDepth.DEPTH_24_BIT


def test_a_forced_depth_beats_the_probe():
    detect = detection(
        term="xterm-256color", forced=ColorDepth.DEPTH_4_BIT
    )
    detect.handle_reply("\x1bP1$r38;2;1;2;3m\x1b\\")
    assert detect.truecolor  # The probe still came back.
    assert detect.depth == ColorDepth.DEPTH_4_BIT  # But the flag wins.
