"""
A grapheme cluster fitted to the outer terminal. Lillecarl/pymux#798.

The XTVERSION answers are the ones four real terminals gave pymux.
"""

from __future__ import annotations

import pytest

from pymux.clusters import CLUSTER_WIDTH_PROBE, PROBES, Drawn, Kind, fitted, kind_of, term_program_of

FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
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
    frame = "中❤\ufe0f"
    assert fitted(frame, "xterm") is frame


def _answered(*reports: tuple[int, int]) -> Drawn:
    drawn = Drawn()
    for row, column in reports:
        drawn = drawn.answered(row, column)
    return drawn


def test_the_probe_writes_each_cluster_on_its_own_row():
    for row, (_, cluster) in PROBES.items():
        assert "\x1b[%d;1H%s\x1b[6n" % (row, cluster) in CLUSTER_WIDTH_PROBE


def test_a_measured_terminal_is_fitted_by_its_answers():
    "WezTerm's answers: the flag one column wide."
    drawn = _answered((3, 3), (4, 2), (5, 3))
    assert drawn == Drawn(family=2, flag=1, tone=2)
    assert drawn.differs()
    assert fitted("a" + FLAG + "b" + FAMILY, drawn) == "a" + FLAG + " b" + FAMILY


def test_a_terminal_that_draws_the_probe_as_pieces_draws_every_cluster_so():
    "xterm's family is three faces, so a family of two is two faces."
    drawn = _answered((3, 7))
    assert drawn.width(FAMILY[2:]) == 4
    assert fitted(FAMILY[2:] + "b", drawn) == FAMILY[2] + "b"


def test_a_kind_the_terminal_did_not_answer_for_is_left_as_it_is():
    drawn = _answered((4, 2))
    assert fitted(THUMB + FLAG, drawn) == THUMB + FLAG + " "


def test_answers_that_agree_with_the_pane_change_nothing():
    assert not _answered((3, 3), (4, 3), (5, 3)).differs()
    assert not Drawn().differs()


def test_a_toned_family_counts_as_a_family():
    assert kind_of(FAMILY[0] + THUMB[1] + FAMILY[1:]) is Kind.FAMILY
    assert kind_of(FLAG) is Kind.FLAG
    assert kind_of(THUMB) is Kind.TONE
