"""
What a client asks its own terminal about colour (`pymux.colors`).

Two questions, and neither is about the session: how many colours the
terminal takes, and which two it draws with when nothing says
otherwise.
"""

import asyncio
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import ColorDepth

from pymux.colors import (
    COLOR_QUERIES,
    TRUECOLOR_PROBE,
    ColorDetection,
    DefaultColors,
    depth_from_environment,
    reports_truecolor,
    theme_color_base,
)
from pymux.client.terminal import DETECTION_QUERIES
from pymux.main import Pymux
from pymux.server import ServerConnection
from pyte import escape
from pyte.colors import DEFAULT_COLORS, PALETTE, Color
from pyte.osc import COLOR_BASE
from pyte.screen import Screen
from pyte.sequences import Csi, apc, csi, dcs, osc
from test_server_tasks import FakePipe, FakePymux


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
        dcs("1$r38;2;1;2;3m"),
        # The colon form, with the empty colour space id.
        dcs("1$r38:2::1:2:3m"),
        # The colon form without the colour space id.
        dcs("1$r38:2:1:2:3m"),
        # The colon form with a colour space id that is not empty.
        dcs("1$r38:2:0:1:2:3m"),
        # Other attributes around it.
        dcs("1$r0;1;38;2;1;2;3;48;5;16m"),
        # Terminals disagree about the validity digit.
        dcs("0$r38;2;1;2;3m"),
        dcs("$r38;2;1;2;3m"),
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
        dcs("1$r38;5;16m"),
        # It dropped the colour.
        dcs("1$r0m"),
        # It kept a colour, but not the one that was asked for.
        dcs("1$r38;2;4;5;6m"),
        # Not a DECRQSS reply at all.
        csi(escape.DA, 62, 1, 6, private="?"),
        apc("Gi=31;OK"),
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

    detect.handle_reply(dcs("1$r38:2::1:2:3m"))
    assert detect.truecolor
    assert detect.depth == ColorDepth.DEPTH_24_BIT


def test_an_unrelated_reply_changes_nothing():
    detect = detection(term="xterm-256color")
    detect.handle_reply(csi(Csi.XTWINOPS, 6, 20, 10))
    detect.handle_reply(apc("Gi=31;OK"))
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
    detect = detection(term="xterm-256color", forced=ColorDepth.DEPTH_4_BIT)
    detect.handle_reply(dcs("1$r38;2;1;2;3m"))
    assert detect.truecolor  # The probe still came back.
    assert detect.depth == ColorDepth.DEPTH_4_BIT  # But the flag wins.


# ----------------------------------------------------------------------
# Which two colours the terminal draws with.
#
# The other half of what a client asks its terminal: not how many
# colours it takes, but which two it uses when nothing says otherwise.
# pymux draws its status line and its title bars over that background,
# so what it is decides whether its own colours land or fight.
# Lillecarl/pymux#223.


def test_the_queries_ask_for_the_two_defaults_and_the_sixteen_ansi():
    "The cube beyond sixteen is convention, so it is not asked about."
    assert COLOR_QUERIES == osc("10", "?") + osc("11", "?") + "".join(
        osc("4", "%i;?" % index) for index in range(16)
    )


def test_a_fresh_terminal_has_said_nothing():
    colors = DefaultColors()
    assert colors.foreground is None
    assert colors.background is None


def test_a_background_reply_is_read():
    colors = DefaultColors()

    assert colors.handle_osc_reply("11", "rgb:1e1e/1e1e/2e2e")

    assert colors.background == Color(0x1E, 0x1E, 0x2E)
    assert colors.foreground is None


def test_a_foreground_reply_is_read():
    colors = DefaultColors()

    assert colors.handle_osc_reply("10", "#ffffff")

    assert colors.foreground == Color(0xFF, 0xFF, 0xFF)


def test_the_second_reply_of_a_colour_replaces_the_first():
    "A terminal whose theme changed says so the same way it answered."
    colors = DefaultColors()
    colors.handle_osc_reply("11", "rgb:0000/0000/0000")
    colors.handle_osc_reply("11", "rgb:ffff/ffff/ffff")

    assert colors.background == Color(0xFF, 0xFF, 0xFF)


@pytest.mark.parametrize(
    "code, payload",
    [
        # A colour pymux does not draw with: the cursor, and the
        # selection. `DYNAMIC_COLOR_CODES` numbers them and this reads
        # neither.
        ("12", "rgb:ffff/0000/0000"),
        ("17", "rgb:ffff/0000/0000"),
        # Another code entirely.
        ("99", "i=1"),
        # A spec XParseColor does not name.
        ("11", "aubergine"),
        ("11", ""),
    ],
)
def test_a_reply_this_cannot_read_changes_nothing(code, payload):
    colors = DefaultColors()

    assert not colors.handle_osc_reply(code, payload)

    assert colors.background is None
    assert colors.foreground is None


# ----------------------------------------------------------------------
# The sixteen colours the terminal paints the palette with.
#
# The pane answers the queries of its program with what this terminal
# paints, so the handshake asks for the theme that the person is
# looking at. Lillecarl/pymux#283.


def test_an_ansi_reply_is_read():
    colors = DefaultColors()

    assert colors.handle_osc_reply("4", "1;rgb:ffff/0000/0000")

    assert colors.ansi[1] == Color(0xFF, 0x00, 0x00)


def test_an_ansi_reply_beyond_the_sixteen_changes_nothing():
    # The ask named the first sixteen only. A cube entry is convention
    # in every terminal, and a reply for it is one nobody asked for.
    colors = DefaultColors()

    assert not colors.handle_osc_reply("4", "20;rgb:ffff/0000/0000")
    assert not colors.handle_osc_reply("4", "aubergine;rgb:ffff/0000/0000")


def test_a_color_base_carries_the_learned_sixteen_over_the_cube():
    colors = DefaultColors()
    red = Color(0xFF, 0x00, 0x00)
    blue = Color(0x00, 0x00, 0xFF)
    colors.handle_osc_reply("4", "1;%s" % red.spec)
    colors.handle_osc_reply("4", "14;%s" % blue.spec)

    base = colors.color_base()

    assert base.palette[:16] == [
        red if index == 1 else blue if index == 14 else PALETTE[index]
        for index in range(16)
    ]
    assert base.palette[16:] == list(PALETTE[16:])


def test_a_color_base_carries_the_learned_defaults():
    colors = DefaultColors()
    colors.handle_osc_reply("10", "rgb:ffff/0000/0000")
    colors.handle_osc_reply("11", "rgb:0000/0000/ffff")

    base = colors.color_base()

    assert base.defaults["foreground"] == Color(0xFF, 0x00, 0x00)
    assert base.defaults["background"] == Color(0x00, 0x00, 0xFF)
    assert base.defaults["cursor"] == DEFAULT_COLORS["cursor"]


def test_a_color_base_with_nothing_learned_is_the_conventional_one():
    base = DefaultColors().color_base()

    assert base.palette == list(PALETTE)
    assert base.defaults == DEFAULT_COLORS
    assert base == COLOR_BASE


# ----------------------------------------------------------------------
# The theme, as what a pane answers with.
#
# With `paint-screen` on, the theme colours the whole screen, and the
# palette a program asks for is part of that screen.


def test_the_theme_gives_a_whole_palette():
    base = theme_color_base("grey")

    assert len(base.palette) == 256
    assert base.palette[16:] == list(PALETTE[16:])
    assert base.defaults["foreground"] == Color(0xD0, 0xD0, 0xD0)
    assert base.defaults["background"] == Color(0x00, 0x00, 0x00)


def test_a_pygments_theme_names_the_pane_s_red():
    base = theme_color_base("pygments:dracula")

    assert base.palette[1] == Color(0x8B, 0x08, 0x0B)
    assert base.defaults["background"] == Color(0x28, 0x2A, 0x36)


def test_a_theme_that_owns_the_screen_gives_the_pane_its_palette():
    pymux = Pymux()
    pymux.paint_screen = True
    pymux.theme = "pygments:dracula"
    pane = SimpleNamespace(screen=Screen(24, 80, write_process_input=lambda data: None))

    pymux.tell_pane_about_the_colours(pane)

    assert pane.screen.color_base.palette[1] == Color(0x8B, 0x08, 0x0B)


def test_a_pane_keeps_the_convention_while_the_terminal_owns_the_colours():
    pymux = Pymux()
    assert pymux.paint_screen is False
    screen = Screen(24, 80, write_process_input=lambda data: None)
    pane = SimpleNamespace(screen=screen)

    pymux.tell_pane_about_the_colours(pane)

    # Nobody attached, and the theme does not own the screen: the pane
    # stays on what `pyte` reports, as before any of this.
    assert pane.screen.color_base == COLOR_BASE


# ----------------------------------------------------------------------
# And the reply reaches the client it belongs to.


def test_the_detection_asks_the_outer_terminal_for_its_colours():
    "Before the device attributes, which is the fence of the detection."
    queries = DETECTION_QUERIES.decode("ascii")

    assert COLOR_QUERIES in queries
    assert queries.index(COLOR_QUERIES) < queries.index("\x1b[c")


def test_a_reply_lands_on_the_connection_that_carried_it():
    """
    Each client asks its own terminal and keeps its own answer, so two
    people on one session can be on a light terminal and a dark one.
    """

    async def check():
        one = ServerConnection(FakePymux(), FakePipe())
        other = ServerConnection(FakePymux(), FakePipe())

        one._handle_kitty_reply(osc("11", "rgb:1e1e/1e1e/2e2e"))
        other._handle_kitty_reply(osc("11", "rgb:ffff/ffff/ffff"))

        assert one.default_colors.background == Color(0x1E, 0x1E, 0x2E)
        assert other.default_colors.background == Color(0xFF, 0xFF, 0xFF)

        one._close_connection()
        other._close_connection()

    asyncio.run(check())


def test_a_colour_arriving_after_the_detection_is_still_read():
    "A terminal whose theme changes while a person is attached says so."

    async def check():
        connection = ServerConnection(FakePymux(), FakePipe())
        assert not connection._kitty_detection_pending

        connection._handle_kitty_reply(osc("11", "rgb:0000/0000/0000"))

        assert connection.default_colors.background == Color(0, 0, 0)
        connection._close_connection()

    asyncio.run(check())


# ----------------------------------------------------------------------
# And what the panes answer with follows.

def test_a_learned_colour_tells_the_panes_again():
    "The panes answer their programs with what this terminal paints."

    async def check():
        pymux = FakePymux()
        connection = ServerConnection(pymux, FakePipe())

        connection._handle_kitty_reply(osc("4", "1;rgb:ffff/0000/0000"))
        connection._handle_kitty_reply(osc("11", "rgb:0000/0000/0000"))

        assert pymux.color_base_syncs == 2
        connection._close_connection()

    asyncio.run(check())


def test_a_reply_that_learns_nothing_tells_the_panes_nothing():
    "A reply this cannot read does not send the panes walking."

    async def check():
        pymux = FakePymux()
        connection = ServerConnection(pymux, FakePipe())

        connection._handle_kitty_reply(osc("4", "20;rgb:ffff/0000/0000"))
        # The cursor is a colour pymux does not draw with, and an
        # index beyond the sixteen is one nobody asked for.
        connection._handle_kitty_reply(osc("12", "rgb:ffff/0000/0000"))

        assert pymux.color_base_syncs == 0
        connection._close_connection()

    asyncio.run(check())
