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

from session import Connection
from pymux import introspect, log
from pymux.commands.commands import handle_command
from pymux.enums import Woke
from pymux.main import Pymux
from pymux.server import _SocketStdout

SIZE = Size(rows=24, columns=80)


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
            connection=Connection(),
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
    path = introspect.write_dump(pymux)

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


def test_the_counters_name_what_asked_for_each_frame(a_server):
    """
    The finding a count alone cannot carry.

    "Eleven frames a second" says a server is busy. "Eleven frames a
    second, and every one because an application asked" says what is
    doing it, which is the whole diagnosis.
    """
    pymux, state = a_server

    with set_app(state.app):
        pymux.invalidate(Woke.CLIENT_RESIZED)
        pymux.invalidate(Woke.CLIENT_RESIZED)
        pymux.invalidate(Woke.APPLICATION)

    said = introspect.counters(pymux)

    assert pymux.counters.invalidates[Woke.CLIENT_RESIZED] == 2
    assert str(Woke.CLIENT_RESIZED)[:46] in said
    assert "what asked for a frame" in said


def test_a_frame_that_went_out_is_counted():
    """
    One flush is one frame, and several invalidates become one of them.
    So the frames are counted where they are written and not where they
    are asked for.
    """
    counters = introspect.Counters()
    stdout = _SocketStdout(lambda packet: None, counters)

    stdout.write("hello")
    stdout.write(" there")
    stdout.flush()

    assert counters.frames == 1
    assert counters.frame_bytes == len("hello there")


def test_a_server_with_no_counters_still_writes(a_server):
    "The counting is not what a frame is for."
    sent = []
    stdout = _SocketStdout(sent.append, None)

    stdout.write("hello")
    stdout.flush()

    assert sent == [{"cmd": "out", "data": "hello"}]


def test_the_profile_returns_at_once_and_writes_later(a_server, tmp_path):
    """
    A profile of a server is taken while the server serves.

    So the command asks the loop to stop the profiler later and answers
    with the file it will land in. Stopping the server to look at it
    would measure a different program.
    """
    pymux, _state = a_server

    async def check():
        pymux.loop = asyncio.get_running_loop()
        path = introspect.start_watching(pymux, seconds=0.05)

        assert path.parent == tmp_path
        assert not path.exists(), "it wrote before it had watched"

        await asyncio.sleep(0.3)
        return path

    path = asyncio.run(check())

    written = path.read_text()
    assert "what it did over these 0.1 seconds" in written
    assert "--- asyncio tasks" in written
    assert path.with_suffix(".html").is_file()


def test_nothing_may_attach_to_a_server_nobody_asked_about(a_server):
    """
    `PR_SET_PTRACER_ANY` lets anything this user runs read the server's
    memory, and a server holds every pane's scrollback. So it is off.
    """
    pymux, _state = a_server

    assert pymux.allow_remote_debugging is False
    assert "a debugger may attach: no" in introspect.what_it_is_doing(pymux)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux")
def test_the_option_tells_the_kernel(a_server):
    "It is a setting the kernel has to be told about, so it is a property."
    pymux, state = a_server

    with set_app(state.app):
        handle_command(pymux, "set-option allow-remote-debugging on")
        assert pymux.allow_remote_debugging is True
        assert "a debugger may attach: yes" in introspect.what_it_is_doing(pymux)

        handle_command(pymux, "set-option allow-remote-debugging off")
        assert pymux.allow_remote_debugging is False


def test_every_route_that_starts_a_server_takes_the_signal():
    """
    Three routes start a server, and a person sending a signal does not
    know which one this is.
    """
    source = inspect.getsource(Pymux)

    assert source.count("self.server_starts()") == 3
    assert "introspect.answer_a_signal()" in inspect.getsource(Pymux.server_starts)
