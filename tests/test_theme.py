"""
Choosing a colour scheme.

`pymux.style` was one module-level object, handed to each application
when it was built. There was nowhere to choose another, and nothing
short of restarting the server would have changed one.

`set-client-option theme <name>` chooses one now. `pymux/style.py`
holds them by name, and the application takes the scheme through a
`DynamicStyle`, so it reads the client's own style on every render and
a client that is already attached follows.

**A theme belongs to one client.** Only a client can know what its
terminal is, so two people on one session, one on a light screen and
one on a dark one, choose separately. Lillecarl/pymux#223.

**The tests ask the client, not the option.** `client.theme` says what
was set and nothing else; whether a client draws with it is the
question, and `app.style` is where a client reads it. An application
that was handed the scheme itself passes every test on the attribute
and fails these.

Lillecarl/pymux#194, Lillecarl/pymux#195.
"""

import io
import sys
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from session import Connection
from pymux.main import Pymux
from pymux.nearest import NEAREST
from pymux.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS, SetOptionError
from pymux.style import DEFAULT_THEME, THEMES

ROWS, COLUMNS = 24, 80

#: A pane that ends at once and holds a real screen while it lives.
NOTHING = "%s -c pass" % (sys.executable,)

#: Everything the grey theme replaces. It is written here and not read
#: off the themes: a test that derives the list from the thing it
#: judges says nothing.
#:
#: The last two are not green. A picture of the theme showed the pane
#: index of the focused pane on pure red and its number on orange,
#: which are the two loudest things on a screen whose whole point is
#: that it is quiet. Lillecarl/pymux#161.
LOUD_ONES = frozenset(
    {
        "terminal.focused border",
        "terminal.focused titlebar",
        "terminal.focused titlebar name",
        "statusbar",
        "statusbar window.current",
        "auto-suggestion",
        "message",
        "clock",
        "search-toolbar.prompt",
        "search-toolbar.text",
        "search-match",
        "search-match.current",
        "terminal.focused titlebar paneindex",
        "terminal.focused panenumber",
    }
)


@asynccontextmanager
async def create_client():
    "A server with one client attached, and the client's application."
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=Connection("here", None, pymux, "/dev/pts/8"),
        )
        try:
            with set_app(state.app):
                yield pymux, state
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def bar_of(app):
    "The background colour a client draws the status line on."
    return app.style.get_attrs_for_style_str("class:statusbar").bgcolor


def _another_client(pymux, ttyname="/dev/pts/9"):
    "A second terminal on the same server, with a name `-t` can say."
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        return pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=Connection("here", None, pymux, ttyname),
        )


def _said(pymux, command: str) -> str:
    "What a command answers on the command line."
    pymux.command_output = []
    try:
        pymux.handle_command(command)
        return "\n".join(pymux.command_output)
    finally:
        pymux.command_output = None


# ----------------------------------------------------------------------
# What a client draws with.


async def test_client_draws_with_theme_it_starts_on():
    """
    A client starts on the search, and a client whose terminal has
    said nothing draws the default while it says nothing.
    Lillecarl/pymux#346.
    """
    async with create_client() as (pymux, client):
        assert client.theme == NEAREST
        assert client.theme_in_use == DEFAULT_THEME
        assert bar_of(client.app) == "ansigreen"


async def test_the_active_pane_s_number_reads_louder_than_the_others():
    """
    The number a display-panes draws in the active pane is the one a
    person reads first: it says which pane the next key will hit, in
    the mode that holds the numbers for its stay and out of it. #161
    chose the loud colour for it; the rule was keyed with a class
    nothing ever added, so every number drew the same.
    Lillecarl/pymux#161. Lillecarl/pymux#395.
    """
    async with create_client() as (pymux, client):
        loud = client.app.style.get_attrs_for_style_str(
            "class:terminal.focused class:panenumber"
        )
        quiet = client.app.style.get_attrs_for_style_str("class:panenumber")

        assert loud.bgcolor != quiet.bgcolor


async def test_choosing_theme_reaches_client_that_is_attached():
    "The application reads the scheme again on every render."
    async with create_client() as (pymux, client):
        pymux.handle_command("set-client-option theme grey")

        assert client.theme == "grey"
        assert bar_of(client.app) == "5f5f87"


async def test_choosing_theme_back_puts_green_back():
    async with create_client() as (pymux, client):
        pymux.handle_command("set-client-option theme grey")
        pymux.handle_command("set-client-option theme default")

        assert bar_of(client.app) == "ansigreen"


async def test_every_theme_reaches_client():
    "Whatever is registered, and not only the two this file names."
    async with create_client() as (pymux, client):
        for name, theme in THEMES.items():
            pymux.handle_command("set-client-option theme %s" % name)

            assert (
                bar_of(client.app)
                == theme.get_attrs_for_style_str("class:statusbar").bgcolor
            )


async def test_the_theme_belongs_to_one_client():
    """
    Two people on one session, one on a light screen and one on a dark
    one. The server held one theme before, so they drew the same
    colours. Lillecarl/pymux#223.
    """
    async with create_client() as (pymux, one):
        two = _another_client(pymux)

        pymux.handle_command(
            "set-client-option -t %s theme grey" % (two.connection.name,)
        )

        assert bar_of(one.app) == "ansigreen"
        assert bar_of(two.app) == "5f5f87"


async def test_a_client_says_what_its_own_theme_is():
    async with create_client() as (pymux, one):
        two = _another_client(pymux)
        here, there = one.connection.name, two.connection.name
        pymux.handle_command("set-client-option -t %s theme grey" % (there,))

        assert _said(pymux, "show-client-options -t %s theme" % (there,)) == "grey"
        # The other one never named a theme, and its terminal has said
        # nothing for the search to match.
        assert _said(pymux, "show-client-options -t %s theme" % (here,)) == NEAREST


# ----------------------------------------------------------------------
# The roles the themes are written through.


#: What the rules were before they were written as roles. The rules a
#: scheme draws are what a person sees; this is the pin that says the
#: roles produce exactly them, so the refactor changed no drawing. The
#: body of an overlay pane is empty: it draws like a pane, on the
#: terminal's own background, and a colour there put a slab of chrome
#: behind the program's output.
RULES = {
    "border": "#888888",
    "terminal.focused border": "ansigreen bold",
    "terminal titlebar": "bg:#888888 #ffffff",
    "terminal.focused titlebar": "bg:#448844 #ffffff",
    "terminal.focused titlebar name": "bg:#88aa44 #ffffff",
    "terminal.focused titlebar paneindex": "bg:#ff0000",
    "titlebar neighbour": "#dddddd",
    "commandline": "bg:#4e4e4e #ffffff",
    "commandline.command": "bold",
    "commandline.prompt": "bold",
    "commandline.mode": "bold bg:#5f5f87 #ffffff",
    "statusbar": "noreverse bg:ansigreen #000000",
    "statusbar window": "#ffffff",
    "statusbar window.current": "bg:#44ff44 #000000",
    "auto-suggestion": "bg:#4e5e4e #88aa88",
    "message": "bg:#bbee88 #222222",
    # No role in them: a selection on a reversed pane is turned back
    # whatever the theme's colours are, because the pane's own reverse
    # is what it has to be told from. Lillecarl/pymux#99.
    "reversed-pane selected": "noreverse",
    "reversed-pane incsearch.current": "noreverse",
    "background": "#888888",
    "painted": "bg:#000000",
    # The pane's background moved one step, which is what `tinted`
    # does for every scheme now. Lillecarl/pymux#352.
    "cut": "bg:#141414",
    "clock": "bg:#88aa00",
    "panenumber": "bg:#888888",
    "terminal.focused panenumber": "bg:#aa8800",
    "terminated": "bg:#aa0000 #ffffff",
    "confirmationtoolbar": "bg:#880000 #ffffff",
    "confirmationtoolbar question": "",
    "confirmationtoolbar yesno": "bg:#440000",
    "copy-mode-cursor-position": "bg:ansiyellow ansiblack",
    "search-toolbar.prompt": "bg:#88ff44 #444444",
    "search-toolbar.text": "bg:#88ff44 #000000",
    "search-match": "#000000 bg:#88aa88",
    "search-match.current": "#000000 bg:#aaffaa underline",
    "completion-menu": "bg:#1c1c1c #d0d0d0",
    "completion-menu.completion": "bg:#1c1c1c #d0d0d0",
    "completion-menu.completion.current": "bg:#5f5f87 #ffffff",
    "completion-menu.meta.completion": "bg:#262626 #a8a8a8",
    "completion-menu.meta.completion.current": "bg:#5f5f87 #ffffff",
    "scrollbar.background": "bg:#262626",
    "scrollbar.button": "bg:#5f5f87",
    "commandpalette": "bg:#1c1c1c",
    "commandpalette.titlebar": "bg:#5f5f87 #ffffff",
    "commandpalette.title": "bold bg:#5f5f87 #ffffff",
    "which-key.key": "bold #ffffff",
    "chooser.selected": "bg:#5f5f87 #ffffff",
    "chooser.hint": "#d0d0d0",
    "overlay": "",
    "overlay.titlebar": "bg:#5f5f87 #ffffff",
    "overlay.title": "bold",
    "dialog": "noinherit",
    "dialog.body": "noinherit",
    "dialog frame": "noinherit",
    "dialog.body text-area": "noinherit",
    "dialog.body text-area last-line": "noinherit",
}

#: The fourteen rules grey replaces, which are the roles it differs in.
GREY_ROLES = {
    "focus": "#5f5f87",
    "focus-strong": "#8787af",
    "focus-border": "#8787af",
    "alarm": "#8787af",
    "signal": "#5f5f87",
    "signal-bright": "#8787af",
    "signal-text": "#ffffff",
    "suggestion": "#4e4e5e",
    "suggestion-text": "#8888aa",
    "notice": "#8787af",
    "notice-text": "#ffffff",
    "warn": "#5f5f87",
    "warn-bright": "#5f5f87",
    "search": "#8787af",
    "search-prompt-text": "#ffffff",
    "search-match": "#8888aa",
    "search-match-current": "#5f5f87",
    "search-match-current-text": "#ffffff",
}

def test_roles_produce_rules_default_drew():
    from pymux.style import ROLES, derive

    assert derive(ROLES) == RULES


def test_roles_produce_rules_grey_drew():
    """
    Grey was written as the default with the loud rules replaced. The
    roles it replaces are those seventeen; the rules it produces are
    the same seventeen keys.
    """
    from pymux.style import ROLES, derive

    rules_with_grey_replaced = {
        key: value
        for key, value in {
            **RULES,
            **{
                "terminal.focused border": "#8787af bold",
                "terminal.focused titlebar": "bg:#5f5f87 #ffffff",
                "terminal.focused titlebar name": "bg:#8787af #ffffff",
                "terminal.focused titlebar paneindex": "bg:#8787af",
                "statusbar": "noreverse bg:#5f5f87 #ffffff",
                "statusbar window.current": "bg:#8787af #ffffff",
                "auto-suggestion": "bg:#4e4e5e #8888aa",
                "message": "bg:#8787af #ffffff",
                "clock": "bg:#5f5f87",
                "terminal.focused panenumber": "bg:#5f5f87",
                "search-toolbar.prompt": "bg:#8787af #ffffff",
                "search-toolbar.text": "bg:#8787af #000000",
                "search-match": "#000000 bg:#8888aa",
                "search-match.current": "#ffffff bg:#5f5f87 underline",
            },
        }.items()
    }

    assert derive({**ROLES, **GREY_ROLES}) == rules_with_grey_replaced


def test_pane_painting_is_option():
    """
    The rule is in every theme; the pane's container wears it only
    while `paint-screen` is on, and the default is off: a program that
    relies on the terminal's background through its default cells
    keeps seeing the terminal until a person asks for the whole
    screen. Lillecarl/pymux#273.
    """
    pymux = Pymux()
    # `conftest.py` gives every server in this suite `paint-screen` on,
    # so that a picture shows what a theme does. This is the test of
    # the option itself, so it says which way it wants it.
    ALL_OPTIONS["paint-screen"].set_value(pymux, "off")
    assert pymux.paint_screen is False

    ALL_OPTIONS["paint-screen"].set_value(pymux, "on")
    assert pymux.paint_screen is True

    ALL_OPTIONS["paint-screen"].set_value(pymux, "off")
    assert pymux.paint_screen is False


def test_body_of_overlay_pane_draws_like_pane():
    """
    A pane's background is the terminal's own one: a cell the program
    left at the default background shows what is behind pymux, in an
    overlay pane as in any other. The rule named #1c1c1c, which drew a
    slab of chrome behind the program's output and made the overlay a
    different colour from the panes it floats over.
    """
    assert RULES["overlay"] == ""


# ----------------------------------------------------------------------
# The option itself.


def test_name_nobody_registered_is_refused():
    pymux = Pymux()

    with pytest.raises(SetOptionError) as raised:
        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "nosuchtheme")

    assert "default" in raised.value.message
    assert "grey" in raised.value.message


async def test_name_that_is_refused_leaves_the_theme_alone():
    async with create_client() as (pymux, client):
        with pytest.raises(SetOptionError):
            ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "nosuchtheme")

        assert client.theme == NEAREST


def test_a_name_is_refused_before_a_client_is_asked_for():
    """
    The name is read first, so a name nothing offers is refused the
    same way whether or not anybody is attached. The other order says
    "there is no client" to a person who has made a typo.
    """
    pymux = Pymux()

    with pytest.raises(SetOptionError) as raised:
        ALL_CLIENT_OPTIONS["theme"].set_value(pymux, "nosuchtheme")

    assert "no client" not in raised.value.message


def test_names_are_offered_for_completion():
    """
    Which is what `get_all_values` is for. Every source, because a
    person completes a name out of whichever one they use -- and the
    nearest search reads the same list to know what it may match.
    Lillecarl/pymux#346.
    """
    from pymux.style_base16 import names as base16_names
    from pymux.style_pygments import names

    pymux = Pymux()

    assert ALL_CLIENT_OPTIONS["theme"].get_all_values(pymux) == (
        [NEAREST]
        + sorted(THEMES)
        + ["pygments:%s" % (name,) for name in names()]
        + ["base16:%s" % (name,) for name in base16_names()]
    )


def test_grey_theme_replaces_loud_rules_and_no_others():
    """
    Every rule the grey theme does not name is the default's, so a
    rule added to the default reaches both. Naming them here is what
    says a loud one was missed: a new coloured rule in the default
    shows up as a rule the two agree on.
    """
    default = dict(THEMES["default"].style_rules)
    grey = dict(THEMES["grey"].style_rules)

    assert set(default) == set(grey)
    assert {name for name in default if default[name] != grey[name]} == LOUD_ONES


def test_no_rule_of_grey_theme_is_loud():
    """
    The ones it replaces, read back. No green, and none of the three
    colours a picture caught: pure red, orange, bright green.
    """
    grey = dict(THEMES["grey"].style_rules)

    for name in LOUD_ONES:
        assert "green" not in grey[name]
        for loud in ("#ff0000", "#aa8800", "#44ff44"):
            assert loud not in grey[name]
