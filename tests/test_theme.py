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


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


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
            connection=_Connection(),
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
    pymux = Pymux()

    assert ALL_OPTIONS["theme"].get_all_values(pymux) == sorted(THEMES)


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
