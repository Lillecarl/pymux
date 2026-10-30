"""
Whether the screen in front of the person is light or dark, which the
portal tells the programs in window panes. Lillecarl/pymux#812.

The serving is `test_portal.py`'s, in `checks.pymux-wayland`; this is
the deciding, which needs no bus.
"""

from __future__ import annotations

import pytest
from pyte.colors import Color
from test_theme import create_client as a_client

from pymux.core.main import Pymux
from pymux.options.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS
from pymux.wayland.portal import ColorScheme, color_scheme_of

DARK = "pygments:dracula"
LIGHT = "pygments:default"


@pytest.mark.parametrize(
    ("background", "swapped", "scheme"),
    [
        (Color(0x28, 0x2A, 0x36), False, ColorScheme.DARK),
        (Color(0xF8, 0xF8, 0xF8), False, ColorScheme.LIGHT),
        (Color(0x28, 0x2A, 0x36), True, ColorScheme.LIGHT),
        (None, False, ColorScheme.NO_PREFERENCE),
    ],
)
def test_a_background_decides(background, swapped, scheme):
    assert color_scheme_of(background, swapped) == scheme


@pytest.mark.parametrize(("theme", "scheme"), [(DARK, ColorScheme.DARK), (LIGHT, ColorScheme.LIGHT)])
async def test_a_painted_screen_is_the_theme(theme, scheme):
    async with a_client() as (pymux, client):
        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, theme, client)

        assert pymux.portal.scheme == scheme


async def test_a_swapped_theme_turns_the_scheme_over():
    async with a_client() as (pymux, client):
        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, DARK, client)
        ALL_CLIENT_OPTIONS["swap-light-and-dark-colors"].set_value(pymux, "on", client)

        assert pymux.portal.scheme == ColorScheme.LIGHT


async def test_an_unpainted_screen_is_the_terminal():
    "Behind the panes is the terminal's own background, whatever the chrome draws in."
    async with a_client() as (pymux, client):
        ALL_OPTIONS["paint-screen"].set_value(pymux, "off")
        client.default_colors.background = Color(0xFF, 0xFF, 0xFF)
        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, DARK, client)

        assert pymux.portal.scheme == ColorScheme.LIGHT


def test_nobody_attached_says_no_preference():
    pymux = Pymux()
    pymux.terminals.sync_color_scheme()

    assert pymux.portal.scheme == ColorScheme.NO_PREFERENCE
