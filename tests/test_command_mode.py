"""
Leaving command mode.

`:` opens a command line at the bottom. Escape is the key most people
reach for to back out of a mode, and it did nothing: only Ctrl-C and
Ctrl-G were bound. The typed text stayed and the command line kept the
keyboard. Lillecarl/pymux#157.

`status-keys vi` gives Escape to vi, where it leaves insert mode. tmux
draws the same line, so the binding here is for emacs status keys only.

The tests are coroutines. Focusing the command line starts a background
task, and prompt_toolkit asks the running loop for one.
"""
import asyncio
import functools
import io
import sys
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.key_processor import _Flush, KeyPress
from prompt_toolkit.keys import Keys
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

    pymux does not carry anyio and does not turn on `anyio_mode`, so
    pytest here answers a coroutine test with "async def functions are
    not natively supported". Lillecarl/pymux#87 is the move that would
    make this decorator go away.
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
    "Open the command line and type into it, the way `:` does."
    with set_app(state.app):
        state.app.layout.focus(state.command_buffer)
        state.command_buffer.text = "new-window"


def press(state, *keys):
    """
    Feed keys to the application, and let it act on them.

    The flush is what makes a bare Escape a key. Escape is also the
    first byte of every escape sequence, so the processor holds it back
    until a timeout says no more is coming, and then feeds `_Flush`.
    This is that timeout.
    """
    with set_app(state.app):
        for key in keys:
            state.app.key_processor.feed(KeyPress(key))
        state.app.key_processor.process_keys()
        state.app.key_processor.feed(_Flush)
        state.app.key_processor.process_keys()


def leaves_command_mode(pymux, state, key) -> bool:
    """
    Press a key, and say whether it left command mode.

    It watches `leave_command_mode` rather than the focus. An
    application that never ran has nothing to focus back to, so the
    focus does not move here and says nothing either way. What the
    binding owes is the call.
    """
    left = []
    real = pymux.leave_command_mode

    def watched(**arguments):
        left.append(arguments)
        return real(**arguments)

    pymux.leave_command_mode = watched
    try:
        press(state, key)
    finally:
        pymux.leave_command_mode = real
    return bool(left)


@in_a_loop
async def test_escape_leaves_command_mode():
    async with a_session() as (pymux, state):
        in_command_mode(state)

        assert leaves_command_mode(pymux, state, Keys.Escape)


@in_a_loop
async def test_escape_throws_away_what_was_typed():
    "Leaving is leaving. The line does not wait with the text in it."
    async with a_session() as (pymux, state):
        in_command_mode(state)

        leaves_command_mode(pymux, state, Keys.Escape)

        assert state.command_buffer.text == ""


@in_a_loop
async def test_control_c_still_leaves_command_mode():
    async with a_session() as (pymux, state):
        in_command_mode(state)

        assert leaves_command_mode(pymux, state, Keys.ControlC)


@in_a_loop
async def test_control_g_still_leaves_command_mode():
    async with a_session() as (pymux, state):
        in_command_mode(state)

        assert leaves_command_mode(pymux, state, Keys.ControlG)


@in_a_loop
async def test_escape_does_nothing_outside_command_mode():
    "The pane has the keyboard, so Escape belongs to the program in it."
    async with a_session() as (pymux, state):
        assert not leaves_command_mode(pymux, state, Keys.Escape)


@in_a_loop
async def test_escape_stays_with_vi_when_the_status_keys_are_vi():
    """
    `status-keys vi` gives Escape to vi, where it leaves insert mode.
    The command line stays open, which is what tmux does too.
    """
    async with a_session() as (pymux, state):
        ALL_OPTIONS["status-keys"].set_value(pymux, "vi")
        in_command_mode(state)
        # `sync_vi_state` puts the option on the application before
        # every key press. Nothing presses a key here, so this does it.
        state.app.editing_mode = EditingMode.VI

        assert not leaves_command_mode(pymux, state, Keys.Escape)
