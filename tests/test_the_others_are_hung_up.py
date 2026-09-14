"""
`attach -x`: the other clients leave, and their terminals close.

tmux's `-x` does not end a client differently. The client exits the
same way `-d` makes it exit, and then sends SIGHUP to the process that
started it (`client.c:415`), which usually closes the terminal window
it was in. A pymux client already exits when it is detached, so that
signal is the whole of what `-x` adds. Lillecarl/pymux#347.

The rule about *which* clients is Lillecarl/pymux#345's and is judged
in `test_attach_detaches_the_others.py`. This file judges the message.
"""

import contextvars
import json
import os
import signal
import sys
from contextlib import asynccontextmanager

import anyio
from prompt_toolkit.application.current import set_app

from pymux.client.base import Client
from pymux.client.memory import MemoryClient
from pymux.client.terminal import TerminalClient
from pymux.main import Pymux
from pymux.pipes.memory import connect_in_memory
from pymux.server import ServerConnection

from test_the_link_comes_back import Terminal

#: A pane that stays, for the reason `test_attach_detaches_the_others`
#: gives: a session whose last window ended detaches its own clients.
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


@asynccontextmanager
async def a_server():
    pymux = Pymux()
    pymux.test_mode = True

    async with pymux.running():
        try:
            yield pymux
        finally:
            for connection in list(pymux.connections):
                connection.detach_and_close()
            pymux.stop()
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


class Heard:
    "One client's end of a connection, and every packet it was sent."

    def __init__(self, end) -> None:
        self.end = end
        self.packets: list = []

    async def read_until_closed(self) -> None:
        while True:
            try:
                packet = await self.end.read()
            except Exception:
                return
            self.packets.append(json.loads(packet))

    @property
    def exits(self) -> list:
        return [one for one in self.packets if one.get("cmd") == "exit"]


async def _attach(pymux: Pymux, tasks, hang_up_others: bool = False):
    "One client, and everything the server says to it."
    server_end, client_end = connect_in_memory()

    # A context of its own, which is what both real transports do.
    context = contextvars.copy_context()
    connection = context.run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)

    heard = Heard(client_end)
    tasks.start_soon(heard.read_until_closed)

    client_end.write_nowait(
        json.dumps(
            {
                "cmd": "start-gui",
                "detach-others": hang_up_others,
                "hang-up-others": hang_up_others,
                "color-depth": None,
                "term": "xterm-256color",
                "colorterm": "",
                "hostname": "somewhere-else",
                # A terminal apiece, so `detach-client -t` has a name
                # to select one by.
                "ttyname": "/dev/pts/%d" % (len(pymux.connections),),
                "pid": 4242,
                "data": "",
            }
        )
    )

    with anyio.fail_after(5.0):
        while connection.client_state is None and not connection._closed:
            await anyio.sleep(0.005)

    return connection, heard


async def _until(what, says: str, seconds: float = 5.0) -> None:
    with anyio.fail_after(seconds):
        while not what():
            await anyio.sleep(0.005)


# ----------------------------------------------------------------------
# What the server sends.


async def test_the_others_are_told_to_hang_up():
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            first, heard = await _attach(pymux, tasks)
            second, _ = await _attach(pymux, tasks, hang_up_others=True)

            await _until(lambda: heard.exits, "the exit packet")

            assert heard.exits[-1]["hang-up"] is True
            await _until(lambda: first._closed, "the connection to close")
            assert not second._closed
        tasks.cancel_scope.cancel()


async def test_without_the_flag_nobody_is_told_anything():
    """
    `attach -d` closes the connection and says nothing first. The
    client reads the end of the stream, which is what a detach is.
    """
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            first, heard = await _attach(pymux, tasks)

            second, _ = await _attach(pymux, tasks)
            # The `-d` path, without the hangup.
            second._detach_the_others()

            await _until(lambda: first._closed, "the connection to close")
            assert heard.exits == []
        tasks.cancel_scope.cancel()


async def test_the_packet_arrives_before_the_close():
    """
    **The write has to be awaited.** `_send_packet` spawns it into the
    scope of the connection, and closing cancels that scope, so a send
    followed by a close is a race the client usually loses.
    """
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            first, heard = await _attach(pymux, tasks)

            first.detach_and_close(hang_up=True)

            await _until(lambda: first._closed, "the connection to close")
            assert heard.exits, "the client heard nothing before the close"
        tasks.cancel_scope.cancel()


# ----------------------------------------------------------------------
# The two commands that ask for it.


async def _a_session_with_a_window(pymux: Pymux, name: str):
    session = pymux.create_session(name)
    pymux.create_window(WAITS, session=session)
    return session


async def test_attach_session_x_tells_them_to_hang_up():
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            here = await _a_session_with_a_window(pymux, "here")
            elsewhere = await _a_session_with_a_window(pymux, "elsewhere")

            sitting, heard = await _attach(pymux, tasks)
            asking, _ = await _attach(pymux, tasks)
            pymux.attach_client_to(sitting.client_state, here)
            pymux.attach_client_to(asking.client_state, elsewhere)

            with set_app(asking.client_state.app):
                pymux.handle_command("attach-session -t here -x")

            await _until(lambda: heard.exits, "the exit packet")
            assert heard.exits[-1]["hang-up"] is True
            assert not asking._closed
        tasks.cancel_scope.cancel()


async def test_attach_session_x_detaches_them_too():
    "tmux reads `-x` as `-d` with a harsher message: `if (dflag || xflag)`."
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            here = await _a_session_with_a_window(pymux, "here")
            elsewhere = await _a_session_with_a_window(pymux, "elsewhere")

            sitting, _ = await _attach(pymux, tasks)
            asking, _ = await _attach(pymux, tasks)
            pymux.attach_client_to(sitting.client_state, here)
            pymux.attach_client_to(asking.client_state, elsewhere)

            with set_app(asking.client_state.app):
                pymux.handle_command("attach-session -t here -x")

            await _until(lambda: sitting._closed, "the other client to go")
        tasks.cancel_scope.cancel()


async def test_detach_client_takes_the_same_flag():
    "tmux spells it `-P` there, and sends the same message."
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            here = await _a_session_with_a_window(pymux, "here")

            sitting, heard = await _attach(pymux, tasks)
            asking, _ = await _attach(pymux, tasks)
            pymux.attach_client_to(sitting.client_state, here)

            with set_app(asking.client_state.app):
                pymux.handle_command(
                    "detach-client -P -t %s" % (sitting.client_state.connection.name,)
                )

            await _until(lambda: heard.exits, "the exit packet")
            assert heard.exits[-1]["hang-up"] is True
        tasks.cancel_scope.cancel()


async def test_detach_client_without_it_says_nothing():
    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            here = await _a_session_with_a_window(pymux, "here")

            sitting, heard = await _attach(pymux, tasks)
            asking, _ = await _attach(pymux, tasks)
            pymux.attach_client_to(sitting.client_state, here)

            with set_app(asking.client_state.app):
                pymux.handle_command(
                    "detach-client -t %s" % (sitting.client_state.connection.name,)
                )

            await _until(lambda: sitting._closed, "the other client to go")
            assert heard.exits == []
        tasks.cancel_scope.cancel()


async def test_a_real_client_asks_for_it(monkeypatch, tmp_path):
    """
    The flag on the command line, all the way: `pymux attach -x` sets
    it on the client, the client puts it in the `start-gui` packet, and
    the server tells the client that was there to hang up.
    """
    empty = tmp_path / "pymux.conf"
    empty.write_text("")

    async with anyio.create_task_group() as tasks:
        async with a_server() as pymux:
            here = await _a_session_with_a_window(pymux, "here")
            sitting, heard = await _attach(pymux, tasks)
            pymux.attach_client_to(sitting.client_state, here)

            terminal = Terminal()
            monkeypatch.setattr(sys, "stdin", terminal.stdin)
            monkeypatch.setattr(sys, "stdout", terminal.stdout)

            server_end, client_end = connect_in_memory()
            context = contextvars.copy_context()
            connection = context.run(lambda: ServerConnection(pymux, server_end))
            pymux.connections.append(connection)

            client = MemoryClient(client_end)
            client.config_file = str(empty)
            client.hang_up_others = True
            tasks.start_soon(client.attach, True)

            try:
                await _until(lambda: heard.exits, "the exit packet", 20.0)
                assert heard.exits[-1]["hang-up"] is True
            finally:
                terminal.close()
            tasks.cancel_scope.cancel()


# ----------------------------------------------------------------------
# What the client does with it.


def test_a_client_reads_the_flag_off_the_exit_packet():
    client = TerminalClient()

    client._process(json.dumps({"cmd": "exit", "code": 0, "hang-up": True}).encode())

    assert client.hang_up_asked is True
    assert client.exit_code == 0


def test_a_plain_exit_asks_for_no_hangup():
    client = TerminalClient()

    client._process(json.dumps({"cmd": "exit", "code": 1}).encode())

    assert client.hang_up_asked is False
    assert client.exit_code == 1


def test_the_client_signals_its_parent(monkeypatch):
    """
    The parent is named, not read: a test in a build sandbox is PID 1's
    child, and the guard below would skip the signal for the wrong
    reason.
    """
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, number: sent.append((pid, number)))
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    client = Client()
    client.hang_up_asked = True
    client.hang_up_the_parent()

    assert sent == [(4242, signal.SIGHUP)]


def test_a_client_that_was_not_asked_signals_nobody(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, number: sent.append((pid, number)))

    Client().hang_up_the_parent()

    assert sent == []


def test_a_client_whose_parent_is_init_signals_nobody(monkeypatch):
    """
    A client whose parent has already gone is reparented to init, and
    hanging up init is hanging up a process this client never knew.
    tmux holds the same guard: `ppid > 1`.
    """
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, number: sent.append((pid, number)))
    monkeypatch.setattr(os, "getppid", lambda: 1)

    client = Client()
    client.hang_up_asked = True
    client.hang_up_the_parent()

    assert sent == []


def test_a_parent_that_went_between_is_not_a_fault(monkeypatch):
    def gone(pid, number):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "kill", gone)
    monkeypatch.setattr(os, "getppid", lambda: 4242)

    client = Client()
    client.hang_up_asked = True
    client.hang_up_the_parent()
