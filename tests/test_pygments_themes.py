"""
The themes of pygments, chosen by name.

`set-client-option theme pygments:<name>` picks one of the styles pygments
carries, and every rule of pymux's chrome follows from the handful of
colours the style really offers. What it did not say is derived: the
shades are blends of what it gave, and the text on a colour is the
black or white that reads on it.

The colours pinned here are the ones the styles say, not the ones the
derivation computes: the blends are the derivation's own opinion, and
a test that recomputed them would only test the arithmetic twice. The
pinned ones are the anchors a person can check against pygments.

Lillecarl/pymux#194.
"""

import pytest

from pymux.options import ALL_CLIENT_OPTIONS, SetOptionError
from pymux.style_pygments import names, pygments_theme

# A theme belongs to a client, so every test of one needs a client.
from test_theme import create_client as a_client


def attrs(theme, class_name):
    """
    What the theme draws on one class of pymux's chrome.

    prompt_toolkit keeps a colour without its `#` in the attrs it
    answers with, so every pinned colour here is in that form.
    """
    return theme.get_attrs_for_style_str("class:%s" % (class_name,))


def test_boxes_take_colours_of_scheme():
    theme = pygments_theme("dracula")

    assert attrs(theme, "commandpalette").bgcolor == "282a36"
    assert attrs(theme, "completion-menu").color == "f8f8f2"


def test_text_on_bar_reads():
    """
    The same rule on the two ends of one dial. dracula's keyword is
    #ff79c6, which is far closer to black than to white; friendly's is
    #007020 on a light scheme, where white reads.
    """
    dark = pygments_theme("dracula")
    assert attrs(dark, "statusbar").bgcolor == "ff79c6"
    assert attrs(dark, "statusbar").color == "000000"

    light = pygments_theme("friendly")
    assert attrs(light, "statusbar").bgcolor == "007020"
    assert attrs(light, "statusbar").color == "ffffff"


def test_pane_that_ended_is_never_plain_text():
    """
    dracula leaves `Generic.Error` at the plain foreground: a reader
    who took it literally would draw a pane that ended as if nothing
    had happened. The deleted-diff token is the red it does carry, and
    the pane that ended is drawn on it.
    """
    theme = pygments_theme("dracula")

    assert attrs(theme, "terminated").bgcolor == "8b080b"
    assert attrs(theme, "terminated").bgcolor != "f8f8f2"


def test_error_hidden_in_background_is_found():
    """
    solarized-light has no error colour at all in the foreground: the
    error and deleted tokens are its plain text, and the red is in
    `Token.Error bg:#dc322f`. A red slab is the same signal as red
    text, so the chain takes it.
    """
    theme = pygments_theme("solarized-light")

    assert attrs(theme, "terminated").bgcolor == "dc322f"


def test_name_nobody_offers_is_refused():
    with pytest.raises(KeyError):
        pygments_theme("nosuchtheme")


def test_every_name_option_offers_builds_theme():
    """
    All fifty-three: the styles pygments ships, and the four flavours
    of the pastel beside them. A derivation that crashed on one of
    them would leave a name the option lists and the session cannot
    draw, so the whole list builds or the test says which one does
    not.
    """
    for name in names():
        assert pygments_theme(name) is not None


def test_pastel_names_colours_of_scheme():
    """
    catppuccin-mocha: surface #181825, mauve for the accent, the
    muted lavender of its comments for the quiet text. Its `Error`
    and `Generic.Error` tokens are the plain foreground, but the
    deleted-diff token carries the flavour's red, and the pane that
    ends is drawn on that. Lillecarl/pymux#195.
    """
    theme = pygments_theme("catppuccin-mocha")

    assert attrs(theme, "commandpalette").bgcolor == "181825"
    assert attrs(theme, "statusbar").bgcolor == "cba6f7"
    assert attrs(theme, "completion-menu").color == "cdd6f4"
    assert attrs(theme, "terminated").bgcolor == "f38ba8"


async def test_option_offers_names_and_refuses_others():
    async with a_client() as (pymux, client):
        values = ALL_CLIENT_OPTIONS["theme"].get_all_values(pymux)
        assert "default" in values and "pygments:dracula" in values

        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "pygments:dracula")
        assert client.theme == "pygments:dracula"

        with pytest.raises(SetOptionError):
            ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "pygments:nosuchtheme")

        with pytest.raises(SetOptionError):
            ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "vim:nosuchtheme")


async def test_client_draws_with_pygments_theme_it_is_given():
    """
    `client.theme` says what was set; whether the client draws with it
    is the question, and `app.style` is where a client reads it.
    """
    async with a_client() as (pymux, client):
        client.theme = "pygments:dracula"

        assert client.style is pygments_theme("dracula")
        assert (
            client.style.get_attrs_for_style_str("class:statusbar").bgcolor == "ff79c6"
        )
