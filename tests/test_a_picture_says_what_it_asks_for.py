"""
Every picture fixture says the arrangement its keys build, and the
farm asks the server before it keeps the picture.

The fence a fixture ends in proves pymux finished the keys. It does
not prove a key arrived: it travels through the forwarder pane, which
is alive whether or not a split happened. One run of
`cut-follows-the-terminal` drew one pane where the fixture asks for
two, stayed green, and its server log has no second process in it.
Lillecarl/pymux#353.
"""

import pytest

from photograph_chrome import FIXTURES as CHROME_FIXTURES
from photograph_chrome import Fixture, chosen_by_name, count_panes, exact_list
from photograph_themes import FIXTURES as THEME_FIXTURES

#: What `#{pane_mode}` can answer: tmux's two names, and nothing.
MODES = ("", "copy-mode", "clock-mode")


def test_nothing_running_counts_as_no_windows():
    assert count_panes("") == ()


def test_one_line_a_pane_holding_the_number_of_its_window():
    assert count_panes("0\n0\n") == (2,)
    assert count_panes("0\n1\n1\n0\n2\n") == (2, 2, 1)


def test_the_windows_are_sorted_as_numbers():
    """
    Ten windows, sorted as text, put window 10 between 1 and 2, and
    every count after it belongs to the wrong window.
    """
    listing = "\n".join(str(number) for number in range(11))
    assert count_panes(listing) == (1,) * 11


def test_a_knob_that_names_nothing_narrows_nothing(monkeypatch):
    """
    None, and not an empty list: a run that narrowed to nothing would
    take no picture at all and look like a run that found none.
    """
    monkeypatch.delenv("PYMUX_CHROME_LIST", raising=False)
    assert exact_list("PYMUX_CHROME_LIST") is None

    monkeypatch.setenv("PYMUX_CHROME_LIST", "")
    assert exact_list("PYMUX_CHROME_LIST") is None


def test_a_knob_names_fixtures_that_share_no_substring(monkeypatch):
    "Which is the case `PYMUX_CHROME` cannot do. Lillecarl/pymux#365."
    monkeypatch.setenv("PYMUX_CHROME_LIST", "which-key,clock,copy-mode")
    assert exact_list("PYMUX_CHROME_LIST") == ["which-key", "clock", "copy-mode"]


def test_an_exact_list_keeps_the_order_of_what_there_is():
    assert chosen_by_name(["a", "b", "c"], ["c", "a"], "fixture") == ["a", "c"]


def test_one_misspelt_name_in_an_exact_list_stops_the_run():
    """
    And not "did any of them match". A list of four where one is
    misspelt would otherwise run the other three, and a batch that
    quietly shrank is a gap in the gallery nobody sees.
    Lillecarl/pymux#365.
    """
    with pytest.raises(SystemExit) as raised:
        chosen_by_name(["clock", "which-key"], ["clock", "which_key"], "fixture")
    assert "which_key" in str(raised.value)
    # And what there is, because the next thing a person types is the
    # right spelling.
    assert "which-key" in str(raised.value)


def test_a_fixture_asks_for_one_pane_unless_it_says_otherwise():
    assert Fixture("").panes == (1,)


def test_a_fixture_asks_for_no_mode_and_no_prefix_unless_it_says_otherwise():
    """
    The quiet answer is the default, so a fixture that says nothing is
    still judged: a run that left copy mode open or the prefix held
    where none was asked for goes red. Lillecarl/pymux#363.
    """
    assert Fixture("").mode == ""
    assert Fixture("").prefix is False


@pytest.mark.parametrize(
    "name", sorted(CHROME_FIXTURES) + sorted(THEME_FIXTURES)
)
def test_every_fixture_says_what_its_keys_build(name):
    """
    A fixture written as a bare tuple has no `panes`, and a farm that
    reached for one would fail on the fixture rather than judge it.
    """
    fixture = {**CHROME_FIXTURES, **THEME_FIXTURES}[name]
    assert isinstance(fixture, Fixture)
    assert fixture.panes
    assert all(one >= 1 for one in fixture.panes)
    # A mode is named the way tmux names one, and nothing else is a
    # mode: pymux draws the chooser and the palette over a pane too,
    # and part two of Lillecarl/pymux#363 is what to call those.
    assert fixture.mode in MODES
    assert isinstance(fixture.prefix, bool)
