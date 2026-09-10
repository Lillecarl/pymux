"""
What a running server says when it is asked what it is doing.

A server is a daemon a person leaves running for weeks, and nothing
could ask it anything until `pymux/introspect.py`. These say that the
answer holds the two things a person needs -- where the threads are, and
what every asyncio task waits for -- and that a server takes the signal
that answers when its loop is too wedged to read a command.
Lillecarl/pymux#249.
"""

import asyncio
import faulthandler
import inspect
import io
import os
import signal
import sys

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux import introspect, log
from pymux.commands.commands import handle_command
from pymux.main import Pymux

SIZE = Size(rows=24, columns=80)


class _Connection:
    "What `Pymux` asks a connection for, and nothing else."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None


@pytest.fixture
def a_server(tmp_path, monkeypatch):
    """
    A server with one client and one pane, in a state directory of its
    own.

    The pane runs a program that exits at once rather than a shell, so
    that a test leaves no shell of its own behind: `startup` opens a
    window for the first client, and `startup_command` is what goes in
    it.
    """
    monkeypatch.setattr(log, "_logfile", tmp_path / "server.log")

    pymux = Pymux(startup_command="%s -c pass" % (sys.executable,))

    output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: SIZE)
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
            pymux.stop()


def test_a_dump_goes_beside_the_log(a_server, tmp_path):
    "A dump is read with the log around it, so it lives in the same place."
    pymux, _state = a_server
    path = introspect.a_dump(pymux)

    assert path.parent == tmp_path
    assert path.is_file()


def test_a_dump_says_which_server_it_is(a_server):
    "A person reading a dump has several servers, and has to tell them apart."
    pymux, _state = a_server
    said = introspect.what_it_is_doing(pymux)

    assert "pymux server %d" % (os.getpid(),) in said
    assert "1 clients, 1 windows, 1 panes" in said


def test_a_dump_holds_every_thread(a_server):
    pymux, _state = a_server
    said = introspect.what_it_is_doing(pymux)

    assert "--- threads (" in said
    # The thread this test runs on is in it, and so is this frame.
    assert "test_a_dump_holds_every_thread" in said


def test_a_dump_says_what_a_task_waits_for(a_server):
    """
    The half a thread dump cannot show.

    One thread runs the loop, so a thread dump of a server says "the
    loop is polling" and no more. The work of a server is in its tasks.
    """
    pymux, _state = a_server

    async def check():
        async def waits_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(waits_forever(), name="a-task-of-the-test")
        await asyncio.sleep(0)

        pymux.loop = asyncio.get_running_loop()
        said = introspect.what_it_is_doing(pymux)

        task.cancel()
        return said

    said = asyncio.run(check())

    assert "task a-task-of-the-test: pending in " in said
    assert "waits_forever" in said
    # And where it waits, which is the part that names the hang.
    assert "await asyncio.Event().wait()" in said


def test_a_dump_of_a_server_that_never_ran_says_so(a_server):
    "No loop is a state a dump reports, not one it raises on."
    pymux, _state = a_server
    said = introspect.what_it_is_doing(pymux)

    assert "--- asyncio tasks" in said
    assert "task " not in said


def test_the_command_writes_a_dump_and_says_where(a_server, tmp_path):
    pymux, state = a_server
    pymux.command_output = []

    with set_app(state.app):
        handle_command(pymux, "dump-stacks")

    written = pymux.command_output
    assert len(written) == 1
    assert written[0].startswith(str(tmp_path))
    assert (tmp_path / written[0].rsplit("/", 1)[-1]).is_file()


def test_a_server_takes_the_signal_that_answers_a_wedged_loop(a_server, tmp_path):
    """
    `SIGUSR1`, through `faulthandler`.

    Not a handler of our own: a Python handler runs between two
    bytecodes, so a loop stuck inside one C call never reaches it, and
    that is the case this signal is for.
    """
    pymux, _state = a_server
    stacks = tmp_path / ("stacks-%d.log" % (os.getpid(),))

    try:
        assert introspect.answer_a_signal() == stacks

        signal.raise_signal(signal.SIGUSR1)
        written = stacks.read_text()
    finally:
        faulthandler.unregister(signal.SIGUSR1)

    assert "Current thread" in written
    assert "test_a_server_takes_the_signal_that_answers_a_wedged_loop" in written


def test_every_route_that_starts_a_server_takes_the_signal():
    """
    Three routes start a server, and a person sending a signal does not
    know which one this is.
    """
    source = inspect.getsource(Pymux)

    assert source.count("self.the_server_starts()") == 3
    assert "introspect.answer_a_signal()" in inspect.getsource(Pymux.the_server_starts)
