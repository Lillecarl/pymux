"""
`set command-palette on` draws the ":" command line in the middle.

The bar along the bottom is one row tall and the whole width, so the
only thing it can show is the text typed so far and whatever the menu
under the cursor can squeeze in. A box in the middle has room for a
completion beside what it means, for a usage line, and for more than
twelve rows of completions. tmux has no such thing.
Lillecarl/pymux#158.

The two draw the same window, so nothing about command mode changes but
where it is. What the tests below read is which container the layout
would draw, and that is the filter of each one.
"""
import asyncio
import functools
import io
import sys
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.containers import ConditionalContainer, Float
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS

ROWS, COLUMNS = 24, 80


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None


def in_a_loop(test):
    """
    Run this test in an event loop of its own.

    Focusing the command line starts a background task, and
    prompt_toolkit asks the running loop for one. pymux carries no
    anyio, so pytest here does not run a coroutine test by itself.
    Lillecarl/pymux#87 is the move that would remove this.
    """

    @functools.wraps(test)
    def run():
        asyncio.run(test())

    return run


@asynccontextmanager
async def a_session():
    "A server with one client and one window."
    pymux = Pymux()
    pymux.create_window("%s -c pass" % (sys.executable,))

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
            yield pymux, state
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def in_command_mode(state):
    "Open the command line, the way `:` does."
    with set_app(state.app):
        state.app.layout.focus(state.command_buffer)


def the_floats(state):
    "Every float of this client's layout, in the order they are drawn."
    return state.layout_manager.layout.floats


def a_float_is_drawn(state, wanted: Float) -> bool:
    "Whether the layout would draw this float now."
    content = wanted.content
    with set_app(state.app):
        if isinstance(content, ConditionalContainer):
            return bool(content.filter())
        return True


def the_palette_float(state) -> Float:
    "The float that holds the box in the middle."
    for one in the_floats(state):
        if one.top == 5 and one.left == 3 and one.xcursor is not True:
            # The keys pop-up has the same inset, so the two are told
            # apart by which one is a `DynamicContainer`.
            if type(one.content.content).__name__ == "DynamicContainer":
                return one
    raise AssertionError("the layout holds no command palette float")


def the_cursor_menu_float(state) -> Float:
    "The completion menu that hangs off the cursor."
    for one in the_floats(state):
        if one.xcursor:
            return one
    raise AssertionError("the layout holds no menu under the cursor")


@in_a_loop
async def test_the_palette_is_off_to_begin_with():
    "A person used to the bar does not have it move without asking."
    async with a_session() as (pymux, state):
        assert not pymux.command_palette

        in_command_mode(state)

        assert not a_float_is_drawn(state, the_palette_float(state))


@in_a_loop
async def test_the_option_draws_the_palette():
    async with a_session() as (pymux, state):
        ALL_OPTIONS["command-palette"].set_value(pymux, "on")

        in_command_mode(state)

        assert a_float_is_drawn(state, the_palette_float(state))


@in_a_loop
async def test_the_palette_is_drawn_only_in_command_mode():
    "The option says where the command line goes, not that it is open."
    async with a_session() as (pymux, state):
        ALL_OPTIONS["command-palette"].set_value(pymux, "on")

        assert not a_float_is_drawn(state, the_palette_float(state))


@in_a_loop
async def test_the_menu_under_the_cursor_steps_aside_for_the_palette():
    """
    The palette holds a menu of its own, and that one takes the height
    of the box. Two menus at once would be one too many.
    """
    async with a_session() as (pymux, state):
        ALL_OPTIONS["command-palette"].set_value(pymux, "on")
        in_command_mode(state)

        assert not a_float_is_drawn(state, the_cursor_menu_float(state))


@in_a_loop
async def test_the_menu_under_the_cursor_stays_for_the_bar():
    async with a_session() as (pymux, state):
        in_command_mode(state)

        assert a_float_is_drawn(state, the_cursor_menu_float(state))


@in_a_loop
async def test_the_completions_of_the_palette_stop_above_the_status_line():
    """
    The menu that hangs under the cursor is twelve rows at the most.
    The one in the box takes what the box has, and stops there: a menu
    that ran off the bottom drew over the status line and half a row.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager
        with set_app(state.app):
            rows = manager._palette_rows()

        # The box starts five rows down and holds a title and the
        # input, so what is left of the pane is what the menu may take.
        with set_app(state.app):
            pane_rows = pymux.get_window_size().rows
        assert rows == pane_rows - 5 - 2


@in_a_loop
async def test_the_command_line_window_is_built_once():
    """
    The layout focuses a control. A fresh one on every render is one it
    never focused, and then the cursor is drawn nowhere and a person
    cannot see where they are typing.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager

        assert manager._command_line_window() is manager._command_line_window()
        assert manager._command_palette() is manager._command_palette()
