"""
The theme a terminal is closest to.

A client asks its terminal what colours it draws with, and pymux holds
hundreds of themes that name the same colours. So the theme of a
terminal is a search, not something to invent. Lillecarl/pymux#346.

**The round trip is what judges the search.** Feed a theme's own
colours in and that theme has to come back: a scheme is exactly zero
away from itself, and anything else scoring lower means the distance
is wrong.
"""

import pytest

from pymux.colors import DefaultColors
from prompt_toolkit.data_structures import Size

from pymux.nearest import (
    NEAREST,
    WEIGHT_BACKGROUND,
    WEIGHT_PALETTE,
    candidates,
    nearest_theme,
    oklab,
    wanted_from,
)
from pymux.style import DEFAULT_THEME, roles_of
from pyte.colors import parse_color

from session import over_connection

#: Three themes, one out of each source pymux offers.
ONE_OF_EACH = ["default", "base16:gruvbox-dark-hard", "pygments:monokai"]

SIZE = Size(rows=24, columns=80)


def _palette_of(name: str) -> list:
    "The sixteen a theme names, as a terminal would report them."
    roles = roles_of(name)
    return [roles["color-%i" % index] for index in range(16)]


def _said(name: str) -> DefaultColors:
    "A terminal that draws with exactly what this theme names."
    roles = roles_of(name)
    colors = DefaultColors()
    colors.ansi = [parse_color(roles["color-%i" % index]) for index in range(16)]
    colors.background = parse_color(roles["pane"])
    colors.foreground = parse_color(roles["text"])
    return colors


# ----------------------------------------------------------------------
# The distance.


def test_white_is_the_lightest_there_is():
    lightness, a, b = oklab("#ffffff")

    assert lightness == pytest.approx(1.0, abs=0.001)
    assert (a, b) == (pytest.approx(0.0, abs=0.001), pytest.approx(0.0, abs=0.001))


def test_black_is_the_darkest_there_is():
    lightness, a, b = oklab("#000000")

    assert (lightness, a, b) == (
        pytest.approx(0.0, abs=0.001),
        pytest.approx(0.0, abs=0.001),
        pytest.approx(0.0, abs=0.001),
    )


@pytest.mark.parametrize(
    "colour,wanted",
    [
        # The three primaries, as Bjorn Ottosson's reference
        # implementation of oklab gives them for linear sRGB 1.0 --
        # which is what "#ff0000" and its two siblings encode.
        ("#ff0000", (0.627955, 0.224863, 0.125846)),
        ("#00ff00", (0.866440, -0.233888, 0.179498)),
        ("#0000ff", (0.452014, -0.032457, -0.311528)),
    ],
)
def test_the_constants_are_the_reference_ones(colour, wanted):
    """
    A port of somebody else's arithmetic is right or it is a different
    formula, and only a published answer says which. These three are
    what makes "the constants below are Bjorn Ottosson's" a fact
    rather than a claim.
    """
    assert oklab(colour) == pytest.approx(wanted, abs=0.0005)


def test_a_colour_is_read_from_either_form():
    assert oklab("#ff0000") == oklab(parse_color("#ff0000"))


def test_two_greys_are_closer_than_a_grey_and_a_red():
    """
    The property the search needs, and the one RGB distance fails:
    #808080 to #7f7f7f is nothing, and #808080 to a red of the same
    RGB distance is obvious.
    """
    grey = oklab("#808080")
    other_grey = oklab("#7f7f7f")
    red = oklab("#ff0000")

    def apart(one, two):
        return sum((a - b) ** 2 for a, b in zip(one, two))

    assert apart(grey, other_grey) < apart(grey, red)


# ----------------------------------------------------------------------
# The search.


@pytest.mark.parametrize("name", ONE_OF_EACH)
def test_a_theme_matches_itself(name):
    found = nearest_theme(wanted_from(_said(name)))

    assert found is not None
    # A scheme that names exactly the same colours is the same answer.
    assert found == name or candidates()[found] == candidates()[name]


def test_a_terminal_that_said_nothing_matches_nothing():
    "Which is what an in-process client is, and what a dumb terminal is."
    assert nearest_theme(wanted_from(DefaultColors())) is None


def test_one_answer_is_enough_to_search_with():
    "Most terminals answer the background and not the sixteen."
    colors = DefaultColors()
    colors.background = parse_color("#1d2021")

    found = nearest_theme(wanted_from(colors))

    assert found is not None
    assert roles_of(found)["pane"].lower() in ("#1d2021", "#1d2021ff")


def test_the_background_weighs_more_than_one_accent():
    "A person sees the screen behind everything and one accent hardly at all."
    assert WEIGHT_BACKGROUND > WEIGHT_PALETTE


# ----------------------------------------------------------------------
# What a client does with it.


def _reply(connection, code: str, payload: str) -> None:
    "One OSC reply from the outer terminal, as the client forwards it."
    connection._handle_kitty_reply("\x1b]%s;%s\x07" % (code, payload))


async def test_a_client_takes_the_theme_of_its_terminal():
    "The whole of Lillecarl/pymux#346, over a connection."
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)
        assert state.theme == NEAREST
        assert state.theme_in_use == DEFAULT_THEME

        for index, colour in enumerate(_palette_of("base16:gruvbox-dark-hard")):
            _reply(state.connection, "4", "%i;%s" % (index, colour))
        _reply(state.connection, "11", roles_of("base16:gruvbox-dark-hard")["pane"])
        _reply(state.connection, "10", roles_of("base16:gruvbox-dark-hard")["text"])

        assert state.matched == "base16:gruvbox-dark-hard"
        assert state.theme_in_use == "base16:gruvbox-dark-hard"


async def test_a_client_that_named_a_theme_is_left_alone():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE, client_options=[["theme", "grey"]])

        _reply(state.connection, "11", roles_of("base16:gruvbox-dark-hard")["pane"])

        assert state.theme == "grey"
        assert state.theme_in_use == "grey"
        assert state.matched is None


async def test_the_match_is_made_again_as_answers_arrive():
    """
    A terminal answers one colour at a time, so a match made on the
    first reply is a match made on a fraction of what it says.
    """
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)

        _reply(state.connection, "11", "#ffffff")
        first = state.matched
        assert first is not None

        _reply(state.connection, "11", roles_of("base16:gruvbox-dark-hard")["pane"])

        assert state.matched != first


async def test_show_client_options_names_what_it_matched():
    async with over_connection() as session:
        state, _ = await session.attach("here", SIZE)
        _reply(state.connection, "11", roles_of("base16:gruvbox-dark-hard")["pane"])

        session.pymux.command_output = []
        try:
            session.pymux.handle_command("show-client-options theme")
            said = list(session.pymux.command_output)
        finally:
            session.pymux.command_output = None

        assert said == ["%s (%s)" % (NEAREST, state.matched)]


def test_every_theme_that_can_be_read_is_a_candidate():
    "A search that skipped a source could never match a person's own theme."
    found = candidates()

    assert len(found) > 300, "the base16 schemes are missing"
    assert any(name.startswith("pygments:") for name in found)
    assert any(name.startswith("base16:") for name in found)
    assert "default" in found
