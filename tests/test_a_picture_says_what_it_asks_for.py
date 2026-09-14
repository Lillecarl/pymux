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
from photograph_chrome import Fixture, count_panes
from photograph_themes import FIXTURES as THEME_FIXTURES


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


def test_a_fixture_asks_for_one_pane_unless_it_says_otherwise():
    assert Fixture("").panes == (1,)


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
