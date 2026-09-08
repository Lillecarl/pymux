"""
The right arrow accepts what the command line is suggesting.

The ":" line draws a suggestion out of its history, in grey, and no
key took it. prompt_toolkit writes the binding -- right, ctrl+e and
ctrl+f, under a filter that wants the cursor at the end of the line --
and loads it from `PromptSession` alone. An application that builds
its own key bindings gets the grey text and no way to accept it.

Lillecarl/pymux#163.
"""

import asyncio
import functools
import io
import sys
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.auto_suggest import Suggestion
from prompt_toolkit.data_structures import Size
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux

ROWS, COLUMNS = 24, 80


class _Connection:
    """
    What `Pymux` asks a connection for.

    A key press invalidates, and an invalidate tells every connection
    about the pointer and the keyboard, so this stub needs more than
    the ones in the tests that press no key.
    """

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
async def a_session():
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


def press(state, key):
    with set_app(state.app):
        state.app.key_processor.feed(KeyPress(key))
        state.app.key_processor.process_keys()


def suggesting(state, typed, rest):
    """
    The command line, with the cursor at the end and a suggestion.

    The suggestion is put there rather than waited for. Making one is
    prompt_toolkit's job and it does it on a background task; what is
    under test here is the key that accepts it.
    """
    with set_app(state.app):
        state.app.layout.focus(state.command_buffer)
        state.command_buffer.text = typed
        state.command_buffer.cursor_position = len(typed)
        state.command_buffer.suggestion = Suggestion(rest)


@in_a_loop
async def test_the_right_arrow_accepts_the_suggestion():
    async with a_session() as (pymux, state):
        suggesting(state, "new-", "window")

        press(state, Keys.Right)

        assert state.command_buffer.text == "new-window"


@in_a_loop
async def test_control_e_and_control_f_accept_it_as_well():
    "The same binding names all three, and a person may reach for any."
    for key in (Keys.ControlE, Keys.ControlF):
        async with a_session() as (pymux, state):
            suggesting(state, "kill-", "pane")

            press(state, key)

            assert state.command_buffer.text == "kill-pane"


@in_a_loop
async def test_the_right_arrow_still_moves_with_no_suggestion():
    "The filter wants a suggestion, so without one the key is the key."
    async with a_session() as (pymux, state):
        with set_app(state.app):
            state.app.layout.focus(state.command_buffer)
            state.command_buffer.text = "abc"
            state.command_buffer.cursor_position = 0

        press(state, Keys.Right)

        assert state.command_buffer.cursor_position == 1
        assert state.command_buffer.text == "abc"


@in_a_loop
async def test_nothing_here_can_take_the_right_arrow_of_a_pane():
    """
    The key a person presses inside a pane belongs to the program in
    it, and this binding must never reach for it.

    The filter is what decides that, so the filter is what is asked. It
    reads the buffer that has the keyboard, and a pane is not one: a
    pane holds a terminal control, so `current_buffer` is a buffer
    nothing typed into and it carries no suggestion.
    """
    async with a_session() as (pymux, state):
        with set_app(state.app):
            # A suggestion on the command line, and the pane focused.
            state.command_buffer.text = "new-"
            state.command_buffer.suggestion = Suggestion("window")
            assert not _the_binding_would_fire(state)

        suggesting(state, "new-", "window")
        with set_app(state.app):
            assert _the_binding_would_fire(state)


def _the_binding_would_fire(state) -> bool:
    "Whether the right arrow reaches the suggestion binding now."
    for binding in state.app.key_bindings.get_bindings_for_keys((Keys.Right,)):
        if binding.handler.__module__.endswith("auto_suggest"):
            return bool(binding.filter())
    raise AssertionError("nothing binds the right arrow to a suggestion")
