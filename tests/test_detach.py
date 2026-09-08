"""
`ctrl+b d` detaches, and the key handler survives losing its client.

A bound key runs its command and then takes the prefix off the client
that pressed it. `detach-client` removes that client, so asking for it
afterwards raised `ValueError` out of the key handler. The terminal was
already back to the shell by then, so the traceback landed on top of
the prompt. Lillecarl/pymux#109.
"""
import asyncio
import io
import sys

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux

ROWS, COLUMNS = 24, 80


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def __init__(self):
        self.detached = False
        self.pymux = None

    def detach_and_close(self) -> None:
        """
        What `ServerConnection.detach_and_close` does to `Pymux`.

        The real one closes the pipe as well, and nothing here reads
        it. Taking the client away is the half that matters: the key
        handler asks for it afterwards.
        """
        self.detached = True
        self.pymux.remove_client(self)


@pytest.fixture
def session():
    "A server with one client and one window."
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    pymux = Pymux()
    pymux.create_window("%s -c pass" % (sys.executable,))

    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    connection = _Connection()
    connection.pymux = pymux
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=connection,
        )
        try:
            yield pymux, state, connection
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()
            loop.close()


def press(pymux, key_name, command, arguments=()):
    "Bind a key to a command and run what the binding runs."
    manager = pymux.key_bindings_manager
    manager.add_custom_binding(key_name, command, list(arguments), needs_prefix=True)
    handler = manager.custom_bindings[(True, key_name)].handler
    handler(None)


def test_a_detach_tells_the_connection(session):
    pymux, state, connection = session

    with set_app(state.app):
        press(pymux, "d", "detach-client")

    assert connection.detached


def test_a_detach_leaves_no_client_behind(session):
    pymux, state, connection = session

    with set_app(state.app):
        press(pymux, "d", "detach-client")

    assert state.app not in pymux.apps


def test_the_prefix_comes_off_a_client_that_stays(session):
    "The ordinary case: the command keeps the client, and the prefix goes."
    pymux, state, connection = session
    state.has_prefix = True

    with set_app(state.app):
        press(pymux, "n", "next-window")

    assert not state.has_prefix
