"""
A client over SSH keeps the attachment when the link drops.

The panes run on the other machine and they survive a link that goes,
so the client shows a notice, counts down and opens the link again.
Lillecarl/pymux#256.

**The round trip runs on a real pty and a real SSH stack.** The client
draws on the slave and the test reads the master, which is how the
notice is judged: the bytes a person would see. `test_ssh_client.py`
says why an asyncssh server stands in for sshd.
"""

import array
import asyncio
import fcntl
import os
import sys
import termios
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

from pymux.client import ssh as ssh_client
from pymux.client.reconnect import Backoff, link_may_come_back, notice, why
from pymux.client.ssh import SshClient
from pymux.main import Pymux

from test_ssh_client import create_key

PANE_COMMAND = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

#: What the server asks the outer terminal for, and the client never
#: does. A test waits for this to know that a frame arrived.
ALTERNATE_SCREEN = "\x1b[?1049h"


# ----------------------------------------------------------------------
# How long to wait.


def test_the_first_wait_is_short():
    "A link that blinked is back before a person notices."
    assert Backoff(random=lambda: 0.0).next() == 0.5


def test_the_wait_doubles():
    waits = Backoff(random=lambda: 0.0)
    assert [waits.next() for _ in range(5)] == [0.5, 1.0, 2.0, 4.0, 8.0]


def test_the_wait_stops_growing():
    waits = Backoff(random=lambda: 0.0)
    assert max(waits.next() for _ in range(50)) == 30.0


def test_a_wait_is_never_the_whole_wait_twice():
    """
    A machine that comes back takes every client it lost at once, and
    they all counted from the same moment.
    """
    waits = Backoff(random=lambda: 1.0)
    assert [waits.next() for _ in range(3)] == [0.25, 0.5, 1.0]


def test_jitter_never_takes_more_than_half():
    for turn in range(200):
        waits = Backoff()
        assert 0.25 <= waits.next() <= 0.5, turn


def test_a_link_that_came_back_waits_from_the_start_again():
    waits = Backoff(random=lambda: 0.0)
    for _ in range(5):
        waits.next()
    waits.reset()
    assert waits.next() == 0.5


# ----------------------------------------------------------------------
# Which failures come back.


def test_a_lost_connection_comes_back():
    import asyncssh

    assert link_may_come_back(asyncssh.ConnectionLost("Connection lost"))


def test_a_machine_that_does_not_answer_comes_back():
    assert link_may_come_back(ConnectionRefusedError(111, "Connection refused"))
    assert link_may_come_back(OSError(101, "Network is unreachable"))
    assert link_may_come_back(TimeoutError())


def test_a_name_that_does_not_resolve_comes_back():
    "A machine behind a VPN that is not up yet. The name arrives with it."
    import socket

    assert link_may_come_back(socket.gaierror(-2, "Name or service not known"))


def test_a_refused_key_never_comes_back():
    "A retry loop that re-offers a key is a machine for locking an account out."
    import asyncssh

    assert not link_may_come_back(asyncssh.PermissionDenied("Permission denied"))


def test_a_host_key_that_does_not_match_never_comes_back():
    import asyncssh

    assert not link_may_come_back(asyncssh.HostKeyNotVerifiable("Unknown host key"))


def test_a_socket_that_is_not_there_never_comes_back():
    "The far machine heard the question and said no. It says no again."
    import asyncssh

    assert not link_may_come_back(asyncssh.ChannelOpenError(2, "Connection refused"))


# ----------------------------------------------------------------------
# What the person reads.


def test_the_notice_names_the_machine_and_the_reason():
    lines = notice("dynhetz", "Connection lost", 4.0)

    assert "dynhetz" in lines[0]
    assert "Connection lost" in lines


def test_the_notice_counts_down():
    assert "4 seconds" in "\n".join(notice("dynhetz", "", 3.2))
    assert "1 second." in "\n".join(notice("dynhetz", "", 0.4))


def test_the_notice_says_both_keys():
    said = "\n".join(notice("dynhetz", "", 4.0))

    assert "any key" in said
    assert "q to leave" in said


def test_a_failure_with_nothing_to_say_is_named_by_its_kind():
    assert why(TimeoutError()) == "TimeoutError"
    assert why(OSError("Network is unreachable")) == "Network is unreachable"


# ----------------------------------------------------------------------
# A machine that was asleep. Lillecarl/pymux#445.


class Link:
    "Something with the one method the suspend watch calls."

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


def moving_clocks(steps):
    """
    A `_clocks` that walks through these `(wall, loop)` pairs.

    The last pair repeats, so a watch that keeps looking sees a
    machine whose clocks agree again -- which is what a machine that
    woke up looks like.
    """
    walked = list(steps)

    def clocks():
        return walked.pop(0) if len(walked) > 1 else walked[0]

    return clocks


async def _watched(client, link, seconds: float = 2.0) -> None:
    "Run the suspend watch until it ends or the time is up."
    with anyio.move_on_after(seconds):
        await client._watch_for_suspend(link)


def a_client() -> SshClient:
    "A client that never connects. Only its clock watch is under test."
    return SshClient("ssh://127.0.0.1/tmp/nowhere.sock")


async def test_a_clock_that_keeps_up_never_ends_the_link(monkeypatch):
    "The ordinary case: both clocks move together, for ever."
    monkeypatch.setattr(ssh_client, "SUSPEND_POLL", 0.01)

    client = a_client()
    client._clocks = moving_clocks(
        [(100.0, 100.0), (101.0, 101.0), (102.0, 102.0), (103.0, 103.0)]
    )
    link = Link()

    await _watched(client, link, seconds=0.2)

    assert not link.aborted


async def test_a_poll_that_ran_late_is_not_a_sleep(monkeypatch):
    """
    A machine under load runs a poll late, and both clocks show it. A
    gap has to be the difference between them, never the length of one.
    """
    monkeypatch.setattr(ssh_client, "SUSPEND_POLL", 0.01)

    client = a_client()
    # Thirty seconds late, and the loop agrees it was thirty seconds.
    client._clocks = moving_clocks([(100.0, 100.0), (130.0, 130.0)])
    link = Link()

    await _watched(client, link, seconds=0.2)

    assert not link.aborted, "a slow poll is not a suspend"


async def test_the_world_moving_without_the_loop_ends_the_link(monkeypatch):
    """
    What a closed lid looks like from inside the process: the wall
    clock jumped an hour and the loop's clock barely moved.
    """
    monkeypatch.setattr(ssh_client, "SUSPEND_POLL", 0.01)

    client = a_client()
    client._clocks = moving_clocks([(100.0, 100.0), (3700.0, 101.0)])
    link = Link()

    await _watched(client, link, seconds=0.2)

    assert link.aborted, "an hour asleep has to end the link"


async def test_a_gap_under_the_threshold_is_left_alone(monkeypatch):
    "Below the threshold nothing happens, so a stepped clock costs nothing."
    monkeypatch.setattr(ssh_client, "SUSPEND_POLL", 0.01)

    client = a_client()
    gap = ssh_client.SUSPEND_GAP - 1.0
    client._clocks = moving_clocks([(100.0, 100.0), (100.0 + gap + 0.01, 100.01)])
    link = Link()

    await _watched(client, link, seconds=0.2)

    assert not link.aborted


# ----------------------------------------------------------------------
# The round trip.


class Held:
    "The connection the stand-in sshd accepted, so a test can drop it."

    connection = None


async def create_ssh_server(where: Path, socket_path: str):
    import asyncssh

    host_key, _ = create_key(where, "host")
    client_key, client_pub = create_key(where, "client")

    class OneSocket(asyncssh.SSHServer):
        def connection_made(self, conn) -> None:
            Held.connection = conn

        def begin_auth(self, username: str) -> bool:
            return True

        def unix_connection_requested(self, dest_path: str):
            return dest_path == socket_path

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=OneSocket,
        server_host_keys=[str(host_key)],
        authorized_client_keys=str(client_pub),
    )
    return server, server.get_addresses()[0][1], str(client_key)


class Terminal:
    """
    A pty for the client to draw on, and what it drew.

    The master is read by a thread of its own. Nothing drains it
    otherwise, and a pty holds only a few kilobytes: the client would
    stop in the middle of a frame.
    """

    def __init__(self, rows: int = 24, columns: int = 80) -> None:
        self.master, self.slave = os.openpty()
        fcntl.ioctl(
            self.slave,
            termios.TIOCSWINSZ,
            array.array("h", [rows, columns, 0, 0]),
        )
        self.stdin = open(self.slave, "rb", buffering=0, closefd=False)
        self.stdout = open(self.slave, "w", closefd=False)

        self._said = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            self._said.append(data)

    @property
    def said(self) -> str:
        return b"".join(self._said).decode("utf-8", "replace")

    def type(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def close(self) -> None:
        for one in (self.stdin, self.stdout):
            try:
                one.close()
            except Exception:
                pass
        for fd in (self.slave, self.master):
            try:
                os.close(fd)
            except OSError:
                pass


class Attached:
    "A server with one window, an SSH client on a pty, and the attach."

    def __init__(self, pymux, terminal, attach, client) -> None:
        self.pymux = pymux
        self.terminal = terminal
        self.attach = attach
        self.client = client

    async def until(self, what, says: str, seconds: float = 20.0) -> None:
        """
        Wait for this to become true.

        **An attach that ended raises what ended it.** Without that a
        client which cannot even connect shows as twenty seconds of
        waiting for something else, and the reason is nowhere.
        """
        ends_at = time.monotonic() + seconds
        while time.monotonic() < ends_at:
            if what():
                return
            if self.attach.done():
                self.attach.result()
                raise AssertionError("The attach ended, waiting for %s." % (says,))
            await asyncio.sleep(0.05)
        raise AssertionError("Waited %ss for %s." % (seconds, says))

    def drop_the_link(self) -> None:
        "`abort` sends no disconnect message, the way a dead network does not."
        Held.connection.abort()


@asynccontextmanager
async def attached(monkeypatch, **how):
    "Everything the round trip needs, torn down afterwards."
    import tempfile

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")

    terminal = Terminal()
    monkeypatch.setattr(sys, "stdin", terminal.stdin)
    monkeypatch.setattr(sys, "stdout", terminal.stdout)

    pymux = Pymux()
    pymux.listen_on_socket(socket_path)

    async with pymux.running():
        pymux.create_window(PANE_COMMAND)
        await asyncio.sleep(0.3)

        server, port, client_key = await create_ssh_server(where, socket_path)

        client = SshClient(
            "ssh://127.0.0.1:%d%s" % (port, socket_path),
            known_hosts=None,
            client_keys=[client_key],
            username="anybody",
        )

        attach = asyncio.ensure_future(client._attach(**how))
        try:
            yield Attached(pymux, terminal, attach, client)
        finally:
            attach.cancel()
            server.close()
            terminal.close()
            pymux.stop()
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


async def test_a_dropped_link_shows_a_notice_and_comes_back(monkeypatch):
    """
    The whole of Lillecarl/pymux#256: attach, lose the link, read the
    notice, and be attached again afterwards.
    """
    async with attached(monkeypatch) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the first attach")
        # The alternate screen, which only the server asks for. The
        # client writes its own detection queries before that, so
        # "anything at all" would not say a frame arrived.
        await it.until(
            lambda: ALTERNATE_SCREEN in it.terminal.said, "the first frame"
        )

        it.drop_the_link()

        await it.until(
            lambda: not it.pymux.connections, "the server to let the old client go"
        )
        await it.until(
            lambda: "lost the server" in it.terminal.said, "the disconnected screen"
        )
        assert "any key to try now" in it.terminal.said

        # A person who knows the link is back does not wait for the
        # countdown.
        it.terminal.type("\r")

        await it.until(lambda: len(it.pymux.connections) == 1, "the second attach")

        # And the server lets it go, the way a detach does.
        for connection in list(it.pymux.connections):
            connection.detach_and_close()
        await asyncio.wait_for(it.attach, 10)


async def test_a_machine_that_slept_comes_back_without_waiting(monkeypatch):
    """
    The whole of Lillecarl/pymux#445, on the real stack.

    **Nothing drops the link here.** The stand-in sshd is untouched and
    the server is still serving; the only thing that changes is that
    this client's wall clock jumped an hour while its loop clock did
    not, which is what a closed lid leaves behind. So the notice and
    the reattach can only have come from the suspend watch.

    Without it the same moment costs four keepalive intervals of a
    frozen screen, because the timers that would notice were asleep
    too.
    """
    async with attached(monkeypatch) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the first attach")
        await it.until(
            lambda: ALTERNATE_SCREEN in it.terminal.said, "the first frame"
        )

        # An hour of sleep, from inside the process. Every later read
        # keeps the same offset, which is a machine that woke up and
        # whose clocks agree again.
        real = it.client._clocks
        it.client._clocks = lambda: (real()[0] + 3600.0, real()[1])

        await it.until(
            lambda: not it.pymux.connections, "the server to let the old client go"
        )
        await it.until(
            lambda: "lost the server" in it.terminal.said, "the disconnected screen"
        )

        it.terminal.type("\r")
        await it.until(lambda: len(it.pymux.connections) == 1, "the second attach")

        for connection in list(it.pymux.connections):
            connection.detach_and_close()
        await asyncio.wait_for(it.attach, 10)


async def test_q_leaves_the_disconnected_screen(monkeypatch):
    "A person who is done with it does not wait for the countdown either."
    async with attached(monkeypatch) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the attach")
        it.drop_the_link()
        await it.until(
            lambda: "lost the server" in it.terminal.said, "the disconnected screen"
        )

        it.terminal.type("q")
        await asyncio.wait_for(it.attach, 10)

        assert not it.pymux.connections


async def test_a_server_that_closes_the_connection_is_not_retried(monkeypatch):
    """
    A detach is the server closing the connection, and it means the
    person is done. Retrying it would make a detach impossible.
    """
    async with attached(monkeypatch) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the attach")
        await it.until(
            lambda: ALTERNATE_SCREEN in it.terminal.said, "the first frame"
        )

        for connection in list(it.pymux.connections):
            connection.detach_and_close()

        await asyncio.wait_for(it.attach, 10)
        assert "lost the server" not in it.terminal.said


async def test_a_client_told_to_hang_up_does_not_come_back(monkeypatch):
    """
    `attach -x` on another terminal tells this one to leave. A client
    that can wait for a link to come back must read that as an ending
    and not as a link that went. Lillecarl/pymux#347.
    """
    async with attached(monkeypatch) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the attach")
        await it.until(lambda: ALTERNATE_SCREEN in it.terminal.said, "the first frame")

        for connection in list(it.pymux.connections):
            connection.detach_and_close(hang_up=True)

        await asyncio.wait_for(it.attach, 10)

        assert it.client.hang_up_asked, "the client never heard the hangup"
        assert "lost the server" not in it.terminal.said


async def test_only_the_first_attach_detaches_the_other_clients(monkeypatch):
    """
    `attach -d` names the clients that were there when the person typed
    it. Somebody who attached while this link was down did not, and a
    reconnect that repeated the flag would throw them off.
    """
    asked = []
    original = SshClient._attached

    async def _attached(self, connection, reader, stdin_fd, detach_others, color_depth):
        asked.append(detach_others)
        return await original(
            self, connection, reader, stdin_fd, detach_others, color_depth
        )

    monkeypatch.setattr(SshClient, "_attached", _attached)

    async with attached(monkeypatch, detach_other_clients=True) as it:
        await it.until(lambda: len(it.pymux.connections) == 1, "the first attach")
        it.drop_the_link()
        await it.until(
            lambda: "lost the server" in it.terminal.said, "the disconnected screen"
        )

        it.terminal.type("\r")
        await it.until(lambda: len(asked) == 2, "the second attach")

    assert asked == [True, False]
