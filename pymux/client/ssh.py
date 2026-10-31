"""
A client that reaches a server on another machine, over SSH.

`pymux -S ssh://carl@dynhetz/tmp/pymux.sock.carl.0 attach` draws the
panes here and runs them there.

**The server gains nothing and knows nothing about this.** It keeps its
unix socket. openssh carries a unix socket over a channel of its own,
`direct-streamlocal@openssh.com`, and `asyncssh` opens one with
`conn.open_unix_connection(path)`. So the far side is a normal sshd and
the packets are the same packets. Lillecarl/pymux#90.

**Why not `ssh host -t pymux attach`.** That starts a shell to start a
client to reach a socket on the far side, and the terminal in the
middle is openssh's. With the client opening the socket itself the pane
is drawn here, so everything this machine has stays reachable: its
clipboard, its files, and the keyboard that is really attached.

**One command runs on the other machine, and it only prints.** Which
socket to open when the address named none is the far side's own
knowledge: the room a server binds in, and which of them is newest,
is what `pymux attach` answers on that machine. So the client runs
`pymux find` there -- a command that prints a path and exits, not a
client that owns a terminal -- and opens the channel to the path it
was given. A discovery that lived here would drift from the one
that binds there, which is how the room changed and this client
kept looking in `/tmp`. Lillecarl/pymux#405.

**It connects when it attaches, not when it is made.** The connection
has to live in the loop that reads it, and `create_client` is called
outside one.

This does not start a server. `ssh://` names a machine that is already
running one; spawning one is the other half of Lillecarl/pymux#90.

**A link that drops is a pause and not the end.** The panes are on the
other machine, so this client shows a notice and opens the link again.
`client/relink.py` and `client/reconnect.py` hold that part.
Lillecarl/pymux#256.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import TYPE_CHECKING, Any, NamedTuple, override
from urllib.parse import urlparse

import anyio
from libpymux.agentic import caller_fields
from libpymux.protocol import READ_SIZE, Field, Message, Packet
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, PacketReader
from prompt_toolkit.input.vt100 import raw_mode

from .audio import AudioClient, sound_devices
from .command import NO_ANSWER
from .defaults import is_ssh_url as is_ssh_url
from .forwards import Forwards
from .reconnect import (
    Backoff,
    why,
)
from .relink import Relink, Restarting
from .ssh_requests import browser_asked, forward_asked, open_asked, reap_forwards, report_forwards
from .terminal import DELETE_EVERY_IMAGE, TerminalClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from asyncssh import SSHClientConnection, SSHReader, SSHWriter
    from prompt_toolkit.output import ColorDepth

    #: An open link: the SSH connection, and the stream of the server's socket over it.
    type Link = tuple[SSHClientConnection, SSHReader[bytes]]

__all__ = [
    "SshClient",
    "SshTarget",
    "ssh_target",
]

#: How often to ask the far machine whether it is still there, in
#: seconds, and how many misses end the connection.
#:
#: **Without this a dropped link never ends.** TCP says nothing about a
#: link that stopped carrying: a NAT box that forgot the flow, a laptop
#: that slept, a network that changed. The read waits for ever, so the
#: notice never comes and there is nothing to retry. asyncssh sends
#: `keepalive@openssh.com`, which is what `ssh -o ServerAliveInterval`
#: sends. Lillecarl/pymux#256.
#:
#: **The wait is four intervals, not three.** asyncssh counts up and
#: ends the connection when the count passes the maximum, so the
#: timers at 5, 10, 15 and 20 seconds make the fourth the one that
#: gives up. Twenty seconds of a frozen screen is the cost of a link
#: that went, and the keepalive costs one small packet per five
#: seconds of silence. openssh's own habit is fifteen, which is
#: chosen for a login session that may sit idle for hours, not for a
#: screen somebody is looking at. Lillecarl/pymux#445.
KEEPALIVE_INTERVAL = 5
KEEPALIVE_MISSES = 3

#: How long a way out waits for a closed connection to finish closing.
#: asyncssh closes through loop callbacks, and one that ran after the
#: scope it belonged to completed a future whose waiter was gone.
#: Lillecarl/pymux#587.
CLOSE_SECONDS = 2.0

#: How long a link has to last before the backoff forgets it, in
#: seconds.
#:
#: **A link that opens and drops at once is not a link that came
#: back.** Resetting the backoff the moment the connect returns makes
#: a flapping server a storm at half a second a try, and the person
#: watches "1 second" for ever. A link that held this long was really
#: there, and the next failure starts from the first wait again.
STABLE_LINK = 5.0

#: What runs on the other machine when the address named no path. It
#: prints the socket a local `pymux attach` would take: the newest
#: server of that user, found by the same code that binds one. One
#: command, one line back.
FIND_COMMAND = "pymux find"


class SshTarget(NamedTuple):
    """
    A machine, and the path of a socket on it.

    `path` is `None` when the address named none. It is filled in after
    connecting, because finding it means asking the other machine.
    """

    host: str
    path: str | None
    username: str | None
    port: int | None


def default_socket(username: str) -> str:
    """
    Where the first server of a user listens.

    The guess that is left when `pymux find` gave nothing: no exec
    channel, or a pymux over there that predates it and answered
    with an error. A server with no name takes the lowest free
    number, so the first one is always `.0`, and most machines have
    exactly one. `SshClient._socket` is what asks rather than
    guesses.
    """
    return "/tmp/pymux.sock.%s.0" % (username,)


def ssh_target(url: str) -> SshTarget:
    """
    Read `ssh://[user@]host[:port][/path/to/socket]`.

    With no path the socket is found after connecting, so the path is
    `None` here and `SshClient._socket` fills it in.
    """
    parsed = urlparse(url)

    if parsed.scheme != "ssh":
        raise ValueError("%r is not an ssh:// address." % (url,))
    if not parsed.hostname:
        raise ValueError("%r names no machine." % (url,))

    path = parsed.path
    if not path or path == "/":
        path = None

    return SshTarget(
        host=parsed.hostname,
        path=path,
        username=parsed.username,
        port=parsed.port,
    )


class SshClient(TerminalClient):
    """
    The terminal side here, the server on another machine.

    Everything but the transport is `TerminalClient`'s, and the
    transport is one SSH channel carrying the same packets a unix
    socket carries.

    **The bodies are coroutines and the two entry points are not.**
    `run_pymux.py` calls `attach` and `run_command` from no loop at
    all, the way it does for a client on a socket. So each one starts a
    loop and runs the coroutine in it, and everything inside -- the
    connection, the reader, the writer and the tasks -- lives in that
    one loop.
    """

    #: This client can forward a port, because it holds the SSH
    #: connection that carries one. The server reads it from the
    #: `start-gui` packet, so that `forward-port` on a session nobody
    #: reaches over SSH says so rather than sending into the void.
    can_forward = True

    #: This link crosses a network, so the frames come compressed:
    #: zstd makes text 6.5x smaller. Lillecarl/pymux#633.
    wire_formats = (MSGPACK_ZSTD, MSGPACK)

    def __init__(self, socket_name: str, **connect_with: Any) -> None:
        super().__init__()
        self.target = ssh_target(socket_name)
        #: What a test overrides: a key to use, and no `known_hosts`.
        #: Nothing here passes any, so a real run reads the agent, the
        #: keys in `~/.ssh` and `known_hosts`, the way `ssh` does.
        self.connect_with = connect_with
        #: The socket that was really opened, once it has been. It is
        #: the address's path, or the one the listing found.
        self.path = self.target.path
        self._writer: SSHWriter[bytes] | None = None
        #: What changed about the forwards when the link came back, for
        #: `_attached` to say once it has a client to say it to.
        #: Lillecarl/pymux#442.
        self._forwards_moved = ""
        #: The link while it is gone. Lillecarl/pymux#256.
        self.relink = Relink(self)
        #: The ports this client was asked to forward. It outlives a
        #: connection on purpose: `connect` opens the wanted set again
        #: on the new one, so a link that dropped gives the tunnels
        #: back with the panes. Lillecarl/pymux#436.
        self.forwards = Forwards()
        #: The loop that writes to the server, for sound from a thread.
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Sound, where this machine can play it. Lillecarl/pymux#657.
        devices = sound_devices()
        self.audio = AudioClient(devices, self._send_from_a_thread) if devices is not None else None

    # ------------------------------------------------------------------
    # The transport.

    @override
    def server_host(self) -> str:
        "The host this client dialed, and not a name the server gives itself."
        return self.target.host

    @override
    def send_packet(self, data: Message) -> None:
        "Send to the server."
        if self._writer is None:
            raise BrokenPipeError("This client is not connected.")
        try:
            self._writer.write(self._encode(data))
        except OSError as error:
            # asyncssh says `OSError` for a channel that is not open
            # for sending. The link went: the same thing a pipe that
            # broke says, and the tasks of an attachment read it that
            # way.
            raise BrokenPipeError(str(error)) from error

    async def connect(self, connect_timeout: float | None = None) -> Link:
        """
        Open the channel to the socket on the far side.

        Returns the connection, so that the caller closes it. It is a
        context manager, and asyncssh closes the channel with it.

        `connect_timeout` bounds the attempt, and only a reconnection
        passes one: `relink.CONNECT_TIMEOUT` says why.
        """
        import asyncssh

        target = self.target

        # Only what the address actually said. asyncssh reads its own
        # defaults -- the agent, `~/.ssh`, `known_hosts`, and the
        # config -- for everything left out, which is what makes this
        # behave like `ssh` without configuring anything twice.
        #
        # The keepalive is the one exception, and it is deliberate: a
        # client that can wait for its link to come back has to learn
        # that the link went, and `ServerAliveInterval` is off by
        # default. So this overrides that key of `~/.ssh/config`.
        asking = dict(self.connect_with)
        asking.setdefault("keepalive_interval", KEEPALIVE_INTERVAL)
        asking.setdefault("keepalive_count_max", KEEPALIVE_MISSES)
        # A bound, when the caller named one, for the reason
        # `CONNECT_TIMEOUT` gives. `setdefault`, so a person who named
        # one in the address or their `~/.ssh/config` keeps it.
        if connect_timeout is not None:
            asking.setdefault("connect_timeout", connect_timeout)
        if target.port is not None:
            asking.setdefault("port", target.port)
        if target.username is not None:
            asking.setdefault("username", target.username)

        connection = await asyncssh.connect(target.host, **asking)
        try:
            path = target.path or await self._socket(connection)
            reader, writer = await connection.open_unix_connection(path)
        except Exception:
            connection.close()
            raise

        self.path = path
        self._writer = writer

        # The listeners of the previous connection died with it, so the
        # wanted set opens again on this one. It raises nothing: a port
        # that has since been taken is a line in `list-forwards`, not a
        # reason to keep the person off their panes.
        #
        # What moved is kept for `_attached` to report, because the
        # server has no client to hang a message on until `start-gui`
        # has been sent. Lillecarl/pymux#442.
        self._forwards_moved = await self.forwards.reopen(connection)

        return connection, reader

    async def _socket(self, connection: SSHClientConnection) -> str:
        """
        Which socket to open, when the address named none.

        **One command runs on the other machine, and it only prints.**
        `pymux find` answers with the socket a local attach would
        take, found by the same `servers_newest_first` that binds and
        attaches on that machine -- the room of Lillecarl/pymux#405
        and the flat place before it, newest first. The client
        guesses no shape at all: a discovery that lived here would
        drift from the one that binds there, which is how the room
        changed and this client kept looking in `/tmp`.

        The channel to the socket is `direct-streamlocal@openssh.com`,
        which is sshd itself. So the far side needs a pymux server, an
        sshd, and a `pymux` on the PATH this session's login shell
        gives -- or a path named in the address.

        The user is the one that was really authenticated, which is
        better than a guess here: `~/.ssh/config` can name a different
        one, and asyncssh has already applied it.
        """
        username = connection.get_extra_info("username")

        try:
            result = await connection.run(FIND_COMMAND)
        except Exception:
            # No exec channel: some servers refuse sessions entirely.
            return default_socket(username)

        # `run` decodes as UTF-8 unless told otherwise.
        assert not isinstance(result.stdout, bytes)
        found = (result.stdout or "").splitlines()
        if result.exit_status == 0 and found:
            return found[0].strip()

        # `find` found nothing, or the pymux over there predates it
        # and answered with an error. The flat first server is the
        # guess that is left; naming the path in the address is the
        # answer that always works.
        return default_socket(username)

    # ------------------------------------------------------------------
    # What a person runs.

    @override
    def run_command(self, command: str, pane_id: str | None = None) -> int:
        return anyio.run(self._run_command, command, pane_id)

    async def _run_command(self, command: str, pane_id: str | None = None) -> int:
        """
        Ask the server to run this command, print what it says, and
        return the exit code. `client/posix.py` reads the same packets.

        **A command does not wait for a link to come back.** A person
        ran one thing and wants the answer or the reason. Only an
        attachment retries, because only an attachment has work on the
        other machine to go back to.
        """
        connection, reader = await self.connect()

        try:
            self.send_packet(
                {
                    Field.CMD: Packet.RUN_COMMAND,
                    Field.DATA: command,
                    Field.PANE_ID: pane_id,
                    # Resolved on this machine, where the agent's
                    # environment is: the far side only forwards it.
                    # `libpymux/agentic.py` holds the names.
                    **caller_fields(),
                }
            )

            exit_code = None
            try:
                async for packet in self._packets(reader):
                    if packet[Field.CMD] == Packet.OUT:
                        sys.stdout.write(packet[Field.DATA])
                        sys.stdout.flush()
                    elif packet[Field.CMD] == Packet.ERR:
                        sys.stderr.write(packet[Field.DATA])
                        sys.stderr.flush()
                    elif packet[Field.CMD] == Packet.EXIT:
                        exit_code = packet[Field.CODE]
            except Exception as error:
                # The link went before the answer arrived. Nothing here
                # knows whether the command ran, so the code has to say
                # that it does not.
                sys.stderr.write("pymux lost the server on %s: %s\n" % (self.target.host, why(error)))
                return 1
            if exit_code is None:
                sys.stderr.write(NO_ANSWER % ("%s:%s" % (self.target.host, self.target.path),))
                return 1
            return exit_code
        finally:
            connection.close()
            with anyio.move_on_after(CLOSE_SECONDS, shield=True):
                await connection.wait_closed()

    @override
    def attach(self, detach_other_clients: bool = False, color_depth: ColorDepth | None = None) -> None:
        anyio.run(self._attach, detach_other_clients, color_depth)

    async def _attach(self, detach_other_clients: bool = False, color_depth: ColorDepth | None = None) -> None:
        """
        Attach the user interface, and return when the person is done
        with it.

        **The first connection is not covered by the retries.** A
        person who has just typed the command is waiting for it and
        wants to read what went wrong, not a countdown. Everything
        after that one interrupts work that is already running, which
        is what makes it worth waiting for.
        """
        stdin_fd = sys.stdin.fileno()
        self._loop = asyncio.get_running_loop()
        connection, reader = await self.connect()
        waits = Backoff()
        cannot = None

        with raw_mode(stdin_fd):
            while True:
                # The moment this attachment began, so the backoff can
                # tell a link that held from one that dropped at once.
                lived_from = anyio.current_time()
                lost = await self._attached(connection, reader, stdin_fd, detach_other_clients, color_depth)

                if lost is None and self.restart_wait is not None:
                    # The server said it is restarting, then closed the
                    # channel. The next one takes over the socket, so
                    # this is a link to open again and not a detach.
                    # The old server's images go: the next one knows
                    # none of them. Lillecarl/pymux#409.
                    until = anyio.current_time() + self.restart_wait
                    self.restart_wait = None
                    if self.placed_images:
                        os.write(sys.stdout.fileno(), DELETE_EVERY_IMAGE)
                        self.placed_images = False
                    lost = Restarting(self.restart_why, until)

                if lost is None:
                    break  # The server closed the connection.

                if self.hang_up_asked:
                    # `attach -x` on another terminal told this client
                    # to leave. The link going while that packet was on
                    # its way is still the end of this attachment:
                    # coming back would be coming back uninvited.
                    # Lillecarl/pymux#347.
                    break

                # **A link that opened and dropped at once is not back.**
                # Resetting here, at the top of every retry, made a
                # server that accepts and drops a storm at the first
                # wait, and the person watched "1 second" for ever.
                if anyio.current_time() - lived_from >= STABLE_LINK:
                    waits.reset()

                try:
                    again = await self.relink.again(stdin_fd, waits, lost)
                except Exception as error:
                    cannot = error
                    break

                if again is None:
                    break  # The person stopped it.

                connection, reader = again

        self._reset_terminal()

        if cannot is not None:
            sys.stderr.write("pymux cannot reach the server on %s: %s\n" % (self.target.host, why(cannot)))
            self.exit_code = 1

    async def _attached(
        self,
        connection: SSHClientConnection,
        reader: SSHReader[bytes],
        stdin_fd: int,
        detach_other_clients: bool,
        color_depth: ColorDepth | None,
    ) -> Exception | None:
        """
        Draw one attachment, until it ends.

        The failure that ended the link, or `None` when the server
        closed the connection. Those two are what a client has to tell
        apart: a server that closes says the person detached, or that
        it is gone, and neither comes back.

        The loop is the same shape as `client/memory.py`: the keyboard
        is read through the loop rather than through `select`, because
        the packets are awaited and one thread cannot do both.
        """
        lost = None
        self.relink.slept_through = None

        async with anyio.create_task_group() as tasks:

            async def read_keyboard() -> None:
                """
                Give the server what the person types, until their
                input side is gone.

                An ended stdin is the end of this attachment: the
                person's terminal is what they see and type on, and
                with its input gone there is nothing left to serve.
                The connection is closed, and the read loop ends the
                way a closed link does, through the `finally` that
                restores the terminal. Lillecarl/pymux#419.
                """
                while True:
                    await anyio.wait_readable(stdin_fd)
                    if not self._process_stdin():
                        connection.close()
                        return

            self.tasks = tasks
            tasks.start_soon(self._while_the_link_holds, read_keyboard)
            tasks.start_soon(self._while_the_link_holds, self._watch_signal)
            tasks.start_soon(self._while_the_link_holds, self._watch_size)
            tasks.start_soon(self._while_the_link_holds, reap_forwards, self)
            # It writes no packet, so it needs no guard against a link
            # that stopped taking them: ending the link is its whole job.
            tasks.start_soon(self.relink.watch_for_suspend, connection)

            try:
                # Inside the try, because the link can go between the
                # connection and the first packet. A failure there is
                # the same failure, and it has to reach the retries
                # rather than leave the terminal on the other screen.
                self._start_gui(detach_other_clients, color_depth)
                # What the table holds after `connect` opened it
                # again. The server's copy is drawn from this, and it
                # has to arrive after `start-gui`, because until then
                # the connection has no client to hang it on.
                #
                # It carries a message only when something moved, so a
                # first attach and a reconnect that changed nothing are
                # both silent. Lillecarl/pymux#442.
                report_forwards(self, self._forwards_moved)
                self._forwards_moved = ""

                async for packet in self._packets(reader):
                    if packet[Field.CMD] == Packet.FORWARD:
                        # Opening one is a coroutine and `_process` is
                        # not, so it goes to the scope that owns this
                        # attachment rather than blocking the reader.
                        tasks.start_soon(
                            self._while_the_link_holds,
                            forward_asked,
                            self,
                            connection,
                            packet,
                        )
                        continue

                    if packet[Field.CMD] == Packet.OPEN and packet.get(Field.FORWARD):
                        # A URL that names loopback, which is this
                        # machine to the browser and the other machine
                        # to the pane that printed it. The port has to
                        # be here before the page is.
                        # Lillecarl/pymux#437.
                        tasks.start_soon(
                            self._while_the_link_holds,
                            open_asked,
                            self,
                            connection,
                            packet,
                        )
                        continue

                    if packet[Field.CMD] == Packet.REPORT_FORWARDS:
                        report_forwards(self)
                        continue

                    if packet[Field.CMD] == Packet.BROWSER:
                        tasks.start_soon(self._while_the_link_holds, browser_asked, self, connection, packet)
                        continue

                    self._process(packet)
            except Exception as error:
                lost = error
            finally:
                # The three readers above end with this scope.
                tasks.cancel_scope.cancel()
                self.tasks = None
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self.set_kitty_flags(0)
                self._restore_modes()
                self._writer = None
                # The listeners are this table's own, so nothing else
                # closes them. Left open they would accept a browser
                # and then carry nothing, which is a worse answer than
                # a refused connection while the link is down. The
                # wanted set stays, and `connect` opens it again.
                self.forwards.close()
                # The program that drove them is on the other side of
                # the link that went.
                self.browsers.stop_all()
                if self.audio is not None:
                    self.audio.close()
                connection.close()
                with anyio.move_on_after(CLOSE_SECONDS, shield=True):
                    await connection.wait_closed()

        if lost is None and self.relink.slept_through is not None:
            # The link ended cleanly because this client ended it. Left
            # alone that reads as a detach, and the panes on the other
            # machine would be abandoned by the one thing that was
            # supposed to go back to them. Lillecarl/pymux#445.
            lost = self.relink.slept_through

        return lost

    def _send_from_a_thread(self, packet: Message) -> None:
        """
        Send from the microphone's thread. asyncssh's writer belongs to
        the loop, so the write goes there; a link that went drops it.
        """
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._send_quietly, packet)

    def _send_quietly(self, packet: Message) -> None:
        with contextlib.suppress(BrokenPipeError):
            self.send_packet(packet)

    async def _while_the_link_holds(self, work: Callable[..., Awaitable[None]], *arguments: object) -> None:
        """
        Run one task of an attachment, and end it quietly when the link
        stops taking packets.

        All three of them write, and the link can go between any two
        writes. Without this the one that loses the race raises out of
        the task group, which is not a fault: the read loop has seen
        the same end and is about to cancel these anyway.
        """
        with contextlib.suppress(BrokenPipeError):
            await work(*arguments)

    async def _packets(self, reader: SSHReader[bytes]) -> AsyncIterator[Message]:
        """
        The packets of the server, one at a time.

        A packet ends at a zero byte and a read can hold any part of
        one, so the tail of a read is the head of the next packet.
        `client/posix.py` splits the same stream the same way.

        **A read that fails is not the end of the stream.** asyncssh
        says `ConnectionLost` for a link that went and gives a clean
        end of file for a channel the other side closed, and those two
        mean opposite things to a client that can reconnect. Measured
        against a real asyncssh server: a deliberate detach and a clean
        `close()` both give end of file, and an aborted connection
        gives `ConnectionLost`. Lillecarl/pymux#256.
        """
        packets = PacketReader()

        while True:
            data = await reader.read(READ_SIZE)

            if not data:
                return

            for packet in packets.feed(data):
                yield packet
