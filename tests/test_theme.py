"""
Choosing a colour scheme.

`pymux.style` was one module-level object, handed to each application
when it was built. There was nowhere to choose another, and nothing
short of restarting the server would have changed one.

`set-option theme <name>` chooses one now. `pymux/style.py` holds them
by name, and the application takes the scheme through a `DynamicStyle`,
so it reads `pymux.style` on every render and a client that is already
attached follows.

**The tests ask the client, not the session.** `pymux.theme` says what
was set and nothing else; whether a client draws with it is the
question, and `app.style` is where a client reads it. An application
that was handed the scheme itself passes every test on `pymux.theme`
and fails these.

Lillecarl/pymux#194, Lillecarl/pymux#195.
"""

import asyncio
import functools
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
from pymux.options import ALL_OPTIONS, SetOptionError
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
THE_LOUD_ONES = frozenset(
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
        "panenumber focused",
    }
)


def in_a_loop(test):
    "pymux carries no anyio, so pytest here runs no coroutine test."

    @functools.wraps(test)
    def run():
        asyncio.run(test())

    return run


@asynccontextmanager
async def a_client():
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
            connection=Connection(),
        )
        try:
            with set_app(state.app):
                yield pymux, state.app
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def bar_of(app):
    "The background colour a client draws the status line on."
    return app.style.get_attrs_for_style_str("class:statusbar").bgcolor


# ----------------------------------------------------------------------
# What a client draws with.


@in_a_loop
async def test_a_client_draws_with_the_theme_it_starts_on():
    async with a_client() as (pymux, app):
        assert pymux.theme == DEFAULT_THEME
        assert bar_of(app) == "ansigreen"


@in_a_loop
async def test_choosing_a_theme_reaches_a_client_that_is_attached():
    "The application reads the scheme again on every render."
    async with a_client() as (pymux, app):
        pymux.handle_command("set-option theme grey")

        assert pymux.theme == "grey"
        assert bar_of(app) == "5f5f87"


@in_a_loop
async def test_choosing_the_theme_back_puts_the_green_back():
    async with a_client() as (pymux, app):
        pymux.handle_command("set-option theme grey")
        pymux.handle_command("set-option theme default")

        assert bar_of(app) == "ansigreen"


@in_a_loop
async def test_every_theme_reaches_a_client():
    "Whatever is registered, and not only the two this file names."
    async with a_client() as (pymux, app):
        for name, theme in THEMES.items():
            pymux.handle_command("set-option theme %s" % name)

            assert (
                bar_of(app) == theme.get_attrs_for_style_str("class:statusbar").bgcolor
            )


# ----------------------------------------------------------------------
# The roles the themes are written through.


#: What the rules were before they were written as roles. The rules a
#: scheme draws are what a person sees; this is the pin that says the
#: roles produce exactly them, so the refactor changed no drawing. The
#: body of an overlay pane is empty: it draws like a pane, on the
#: terminal's own background, and a colour there put a slab of chrome
#: behind the program's output.
THE_RULES = {
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
    "statusbar": "noreverse bg:ansigreen #000000",
    "statusbar window": "#ffffff",
    "statusbar window.current": "bg:#44ff44 #000000",
    "auto-suggestion": "bg:#4e5e4e #88aa88",
    "message": "bg:#bbee88 #222222",
    "background": "#888888",
    "cut": "bg:#303030",
    "clock": "bg:#88aa00",
    "panenumber": "bg:#888888",
    "panenumber focused": "bg:#aa8800",
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
THE_GREY_ROLES = {
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

def test_the_roles_produce_the_rules_the_default_drew():
    from pymux.style import ROLES, derive

    assert derive(ROLES) == THE_RULES


def test_the_roles_produce_the_rules_grey_drew():
    """
    Grey was written as the default with the loud rules replaced. The
    roles it replaces are those seventeen; the rules it produces are
    the same seventeen keys.
    """
    from pymux.style import ROLES, derive

    the_old_rules_grey_replaced = {
        key: value
        for key, value in {
            **THE_RULES,
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
                "panenumber focused": "bg:#5f5f87",
                "search-toolbar.prompt": "bg:#8787af #ffffff",
                "search-toolbar.text": "bg:#8787af #000000",
                "search-match": "#000000 bg:#8888aa",
                "search-match.current": "#ffffff bg:#5f5f87 underline",
            },
        }.items()
    }

    assert derive({**ROLES, **THE_GREY_ROLES}) == the_old_rules_grey_replaced


def test_the_body_of_an_overlay_pane_draws_like_a_pane():
    """
    A pane's background is the terminal's own one: a cell the program
    left at the default background shows what is behind pymux, in an
    overlay pane as in any other. The rule named #1c1c1c, which drew a
    slab of chrome behind the program's output and made the overlay a
    different colour from the panes it floats over.
    """
    assert THE_RULES["overlay"] == ""


# ----------------------------------------------------------------------
# The option itself.


def test_a_name_nobody_registered_is_refused():
    pymux = Pymux()

    with pytest.raises(SetOptionError) as raised:
        ALL_OPTIONS["theme"].set_value(pymux, "nosuchtheme")

    assert "default" in raised.value.message
    assert "grey" in raised.value.message


def test_a_name_that_is_refused_leaves_the_theme_alone():
    pymux = Pymux()

    with pytest.raises(SetOptionError):
        ALL_OPTIONS["theme"].set_value(pymux, "nosuchtheme")

    assert pymux.theme == DEFAULT_THEME


def test_the_names_are_offered_for_completion():
    "Which is what `get_all_values` is for."
    from pymux.style_pygments import the_names

    pymux = Pymux()

    assert ALL_OPTIONS["theme"].get_all_values(pymux) == sorted(THEMES) + [
        "pygments:%s" % (name,) for name in the_names()
    ]


def test_the_grey_theme_replaces_the_loud_rules_and_no_others():
    """
    Every rule the grey theme does not name is the default's, so a
    rule added to the default reaches both. Naming them here is what
    says a loud one was missed: a new coloured rule in the default
    shows up as a rule the two agree on.
    """
    default = dict(THEMES["default"].style_rules)
    grey = dict(THEMES["grey"].style_rules)

    assert set(default) == set(grey)
    assert {name for name in default if default[name] != grey[name]} == THE_LOUD_ONES


def test_no_rule_of_the_grey_theme_is_loud():
    """
    The ones it replaces, read back. No green, and none of the three
    colours a picture caught: pure red, orange, bright green.
    """
    grey = dict(THEMES["grey"].style_rules)

    for name in THE_LOUD_ONES:
        assert "green" not in grey[name]
        for loud in ("#ff0000", "#aa8800", "#44ff44"):
            assert loud not in grey[name]
