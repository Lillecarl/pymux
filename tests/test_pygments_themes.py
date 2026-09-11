"""
The themes of pygments, chosen by name.

`set-option theme pygments:<name>` picks one of the styles pygments
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

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS, SetOptionError
from pymux.style_pygments import a_pygments_theme, the_names


def the_attrs(theme, class_name):
    """
    What the theme draws on one class of pymux's chrome.

    prompt_toolkit keeps a colour without its `#` in the attrs it
    answers with, so every pinned colour here is in that form.
    """
    return theme.get_attrs_for_style_str("class:%s" % (class_name,))


def test_the_boxes_take_the_colours_of_the_scheme():
    theme = a_pygments_theme("dracula")

    assert the_attrs(theme, "commandpalette").bgcolor == "282a36"
    assert the_attrs(theme, "completion-menu").color == "f8f8f2"


def test_the_text_on_a_bar_reads():
    """
    The same rule on the two ends of one dial. dracula's keyword is
    #ff79c6, which is far closer to black than to white; friendly's is
    #007020 on a light scheme, where white reads.
    """
    dark = a_pygments_theme("dracula")
    assert the_attrs(dark, "statusbar").bgcolor == "ff79c6"
    assert the_attrs(dark, "statusbar").color == "000000"

    light = a_pygments_theme("friendly")
    assert the_attrs(light, "statusbar").bgcolor == "007020"
    assert the_attrs(light, "statusbar").color == "ffffff"


def test_a_pane_that_ended_is_never_the_plain_text():
    """
    dracula leaves `Generic.Error` at the plain foreground: a reader
    who took it literally would draw a pane that ended as if nothing
    had happened. The deleted-diff token is the red it does carry, and
    the pane that ended is drawn on it.
    """
    theme = a_pygments_theme("dracula")

    assert the_attrs(theme, "terminated").bgcolor == "8b080b"
    assert the_attrs(theme, "terminated").bgcolor != "f8f8f2"


def test_an_error_hidden_in_a_background_is_found():
    """
    solarized-light has no error colour at all in the foreground: the
    error and deleted tokens are its plain text, and the red is in
    `Token.Error bg:#dc322f`. A red slab is the same signal as red
    text, so the chain takes it.
    """
    theme = a_pygments_theme("solarized-light")

    assert the_attrs(theme, "terminated").bgcolor == "dc322f"


def test_a_name_nobody_offers_is_refused():
    with pytest.raises(KeyError):
        a_pygments_theme("nosuchtheme")


def test_every_name_the_option_offers_builds_a_theme():
    """
    All fifty-three: the styles pygments ships, and the four flavours
    of the pastel beside them. A derivation that crashed on one of
    them would leave a name the option lists and the session cannot
    draw, so the whole list builds or the test says which one does
    not.
    """
    for name in the_names():
        assert a_pygments_theme(name) is not None


def test_the_pastel_names_the_colours_of_the_scheme():
    """
    catppuccin-mocha: surface #181825, mauve for the accent, the
    muted lavender of its comments for the quiet text. Its `Error`
    and `Generic.Error` tokens are the plain foreground, but the
    deleted-diff token carries the flavour's red, and the pane that
    ends is drawn on that. Lillecarl/pymux#195.
    """
    theme = a_pygments_theme("catppuccin-mocha")

    assert the_attrs(theme, "commandpalette").bgcolor == "181825"
    assert the_attrs(theme, "statusbar").bgcolor == "cba6f7"
    assert the_attrs(theme, "completion-menu").color == "cdd6f4"
    assert the_attrs(theme, "terminated").bgcolor == "f38ba8"


def test_the_option_offers_the_names_and_refuses_the_others():
    pymux = Pymux()

    values = ALL_OPTIONS["theme"].get_all_values(pymux)
    assert "default" in values and "pygments:dracula" in values

    ALL_OPTIONS["theme"].set_value(pymux, "pygments:dracula")
    assert pymux.theme == "pygments:dracula"

    with pytest.raises(SetOptionError):
        ALL_OPTIONS["theme"].set_value(pymux, "pygments:nosuchtheme")

    with pytest.raises(SetOptionError):
        ALL_OPTIONS["theme"].set_value(pymux, "vim:nosuchtheme")


def test_a_client_draws_with_the_pygments_theme_it_is_given():
    """
    `pymux.theme` says what was set; whether a client draws with it is
    the question, and `app.style` is where a client reads it.
    """
    pymux = Pymux()

    pymux.theme = "pygments:dracula"

    assert pymux.style is a_pygments_theme("dracula")
    assert pymux.style.get_attrs_for_style_str("class:statusbar").bgcolor == "ff79c6"
