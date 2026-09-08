"""
What "detach-client" does on a route that has nothing to detach.

`pymux standalone` puts the user interface straight on the terminal:
no client, no protocol, no connection. `ctrl+b d` reached
`Pymux.detach_client`, found no connection and did nothing at all. The
screen drew again and the panes kept running, so the only way out was
to end every pane or kill the process.

Lillecarl/pymux#160.
"""
import asyncio
import functools
import io
import sys
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux

ROWS, COLUMNS = 24, 80


def in_a_loop(test):
    "pymux carries no anyio, so pytest here runs no coroutine test."

    @functools.wraps(test)
    def run():
        asyncio.run(test())

    return run


@asynccontextmanager
async def a_standalone_session():
    """
    A `Pymux` set up the way `run_standalone` sets one up.

    `run_standalone` itself is not called, because it ends in
    `app.run()`, which does not return until the thing under test has
    happened. Everything before that line is here.
    """
    pymux = Pymux()
    pymux._runs_standalone = True
    pymux.create_window("%s -c pass" % (sys.executable,))
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=None,
        )
        try:
            yield pymux, state
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


@in_a_loop
async def test_a_detach_ends_a_standalone_session():
    async with a_standalone_session() as (pymux, state):
        assert not pymux.done_f.done()

        with set_app(state.app):
            pymux.handle_command("detach-client")

        assert pymux.done_f.done()


@in_a_loop
async def test_a_detach_asks_every_pane_to_stop():
    """
    The panes were running for the session, and the session has gone.

    It is also what lets the process end: a pane that is still running
    holds a `waitpid` in the executor of the loop, and the interpreter
    waits for that thread. Lillecarl/pymux#109 is the same finding on
    the integrated route.

    The ask is what is read, and not `is_terminated`. That property is
    the backend saying it saw the end of the pty, which happens when
    the loop next runs, so reading it here would be a race and not a
    test.

    **The spy still kills.** A stub that only counts leaves the pane
    running, its `waitpid` holds a thread in the executor of the loop,
    and `asyncio.run` never returns: the test hangs rather than fails,
    which is the same trap in the other direction.
    """
    killed = []

    async with a_standalone_session() as (pymux, state):
        panes = list(pymux.panes_by_id.values())
        assert panes
        for pane in panes:
            pane.process.kill = _a_spy_that_still_kills(
                pane, pane.process.kill, killed
            )

        with set_app(state.app):
            pymux.handle_command("detach-client")

        assert killed == panes


def _a_spy_that_still_kills(pane, kill, killed):
    "Write the pane down, then do what the caller asked for."

    def spy():
        killed.append(pane)
        kill()

    return spy


@in_a_loop
async def test_a_client_over_a_connection_still_detaches():
    """
    The other routes are untouched: a client with a connection has one
    to close, and closing it is not the same as ending the session.
    """
    detached = []

    class _Connection:
        kitty_source_flags = 0
        pointer_shape = None
        graphics = None

        def detach_and_close(self):
            detached.append(True)

        def set_pointer_shape(self, shape):
            pass

        def _send_packet(self, packet):
            pass

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
            with set_app(state.app):
                pymux.handle_command("detach-client")

            assert detached == [True]
            assert not pymux.done_f.done()
        finally:
            pymux.stop()
