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
`client/reconnect.py` holds that part. Lillecarl/pymux#256.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from typing import TYPE_CHECKING, NamedTuple, override
from urllib.parse import urlparse

import anyio
from libpymux.protocol import Field, Framer, Packet
from prompt_toolkit.input.vt100 import raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.agentic import caller_cwd, caller_environment
from pymux.forwarding import (
    MAY_NARROW,
    Direction,
    Forward,
    the_far_side_may_narrow,
    with_port,
)
from pymux.utils import nonblocking

from .defaults import is_ssh_url as is_ssh_url
from .forwards import Forwards
from .reconnect import (
    WAN_INTERVAL,
    Backoff,
    Internet,
    draw,
    link_may_come_back,
    notice,
    watch_internet,
    why,
)
from .terminal import TerminalClient

if TYPE_CHECKING:
    from asyncssh import SSHWriter

__all__ = [
    "SshClient",
    "SshTarget",
    "ssh_target",
]

#: How often to tell the server the terminal has a new size, in
#: seconds. `client/memory.py` says why a poll and not only a signal.
SIZE_INTERVAL = 0.5

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

#: How often to look for a gap between the clocks, in seconds.
SUSPEND_POLL = 1.0

#: How far the world may move ahead of the loop before this client
#: calls the link gone, in seconds.
#:
#: The number only has to be above the jitter of a poll that is late
#: under load, and below the shortest sleep worth noticing. A laptop
#: lid closes for minutes, never for four seconds.
SUSPEND_GAP = 4.0

#: How often the disconnected screen draws its countdown, in seconds.
COUNTDOWN_STEP = 1.0

#: How long one reconnection attempt may last, in seconds.
#:
#: **Without this the screen freezes on the last second of the
#: countdown.** asyncssh passes no timeout to the connect by default,
#: so a network that drops packets without refusing them -- an uplink
#: that went, a route that vanished -- leaves the TCP connect in the
#: kernel's hands for the minute or two `tcp_syn_retries` takes. The
#: countdown has already drawn "1 second" by then, and nothing moves
#: until the connect returns. A bound makes the retry loop turn, and
#: the wait grows the way it is meant to.
#:
#: **Only an attempt after a loss is bounded.** A person who just
#: typed `attach` would rather wait than be told no too soon, so the
#: first connect and a command keep asyncssh's own default. Eight
#: seconds is above a slow handshake -- a mobile link with a second of
#: round trip, a loaded far machine -- and well below the twenty a
#: keepalive already spends before it calls a link dead.
CONNECT_TIMEOUT = 8.0

#: How long a link has to last before the backoff forgets it, in
#: seconds.
#:
#: **A link that opens and drops at once is not a link that came
#: back.** Resetting the backoff the moment the connect returns makes
#: a flapping server a storm at half a second a try, and the person
#: watches "1 second" for ever. A link that held this long was really
#: there, and the next failure starts from the first wait again.
STABLE_LINK = 5.0

#: How often to look for a forward nothing is using, in seconds.
#:
#: It decides only how late a reap is, never whether one happens: the
#: idle time itself is measured against the clock. Half a minute is
#: nothing beside the ten minutes a URL forward is given.
#: Lillecarl/pymux#437.
REAP_POLL = 30.0

#: What a person types to stop trying.
LEAVE = ("q", "Q", "\x03")


class _Left:
    "The person stopped the client. A type, so a result can be told apart."


#: What a try returns when the person stopped the client. A sentinel
#: and not an exception, so nothing mistakes it for a failure to retry.
_LEFT = _Left()


#: What runs on the other machine when the address named no path. It
#: prints the socket a local `pymux attach` would take: the newest
#: server of that user, found by the same code that binds one. One
#: command, one line back.
FIND_COMMAND = "pymux find"


def _how_long(seconds: float) -> str:
    "A sleep, in the largest unit that keeps it a small number."
    if seconds < 90:
        return "%d seconds" % (round(seconds),)
    if seconds < 90 * 60:
        return "%d minutes" % (round(seconds / 60),)
    return "%.1f hours" % (seconds / 3600,)


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

    def __init__(self, socket_name: str, **connect_with) -> None:
        super().__init__()
        self.target = ssh_target(socket_name)
        #: What a test overrides: a key to use, and no `known_hosts`.
        #: Nothing here passes any, so a real run reads the agent, the
        #: keys in `~/.ssh` and `known_hosts`, the way `ssh` does.
        self.connect_with = connect_with
        #: The socket that was really opened, once it has been. It is
        #: the address's path, or the one the listing found.
        self.path = self.target.path
        self._writer: SSHWriter | None = None
        #: What changed about the forwards when the link came back, for
        #: `_attached` to say once it has a client to say it to.
        #: Lillecarl/pymux#442.
        self._forwards_moved = ""
        #: Why this client ended its own link, when it did. Only the
        #: suspend watch sets it, and only `_attached` reads it.
        #: Lillecarl/pymux#445.
        self._slept_through: Exception | None = None
        #: The ports this client was asked to forward. It outlives a
        #: connection on purpose: `_connect` opens the wanted set again
        #: on the new one, so a link that dropped gives the tunnels
        #: back with the panes. Lillecarl/pymux#436.
        self.forwards = Forwards()

    # ------------------------------------------------------------------
    # The transport.

    @override
    def _send_packet(self, data) -> None:
        "Send to the server."
        if self._writer is None:
            raise BrokenPipeError("This client is not connected.")
        try:
            self._writer.write(json.dumps(data).encode("utf-8") + b"\0")
        except OSError as error:
            # asyncssh says `OSError` for a channel that is not open
            # for sending. The link went: the same thing a pipe that
            # broke says, and the tasks of an attachment read it that
            # way.
            raise BrokenPipeError(str(error)) from error

    async def _connect(self, connect_timeout: float | None = None):
        """
        Open the channel to the socket on the far side.

        Returns the connection, so that the caller closes it. It is a
        context manager, and asyncssh closes the channel with it.

        `connect_timeout` bounds the attempt, and only a reconnection
        passes one: `CONNECT_TIMEOUT` says why.
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

    async def _socket(self, connection) -> str:
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
    def run_command(self, command, pane_id=None) -> int:
        return anyio.run(self._run_command, command, pane_id)

    async def _run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command, print what it says, and
        return the exit code. `client/posix.py` reads the same packets.

        **A command does not wait for a link to come back.** A person
        ran one thing and wants the answer or the reason. Only an
        attachment retries, because only an attachment has work on the
        other machine to go back to.
        """
        connection, reader = await self._connect()

        try:
            self._send_packet(
                {
                    Field.CMD: Packet.RUN_COMMAND,
                    Field.DATA: command,
                    Field.PANE_ID: pane_id,
                    # Resolved on this machine, where the agent's
                    # environment is: the far side only forwards it.
                    # `pymux/agentic.py` holds the names.
                    Field.ENVIRONMENT: {**dict(os.environ), **caller_environment()},
                    Field.CWD: caller_cwd(),
                }
            )

            exit_code = 0
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
            return exit_code
        finally:
            connection.close()

    @override
    def attach(self, detach_other_clients: bool = False, color_depth=None) -> None:
        anyio.run(self._attach, detach_other_clients, color_depth)

    async def _attach(self, detach_other_clients: bool = False, color_depth=None) -> None:
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
        connection, reader = await self._connect()
        waits = Backoff()
        cannot = None
        # **Only the first attach detaches the others.** `attach -d`
        # names the clients that were there when the person typed it.
        # Somebody who attached while this link was down did not.
        detach_others = detach_other_clients

        with raw_mode(stdin_fd):
            while True:
                # The moment this attachment began, so the backoff can
                # tell a link that held from one that dropped at once.
                lived_from = anyio.current_time()
                lost = await self._attached(connection, reader, stdin_fd, detach_others, color_depth)
                detach_others = False

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
                    again = await self._link_again(stdin_fd, waits, lost)
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

    async def _attached(self, connection, reader, stdin_fd, detach_other_clients, color_depth):
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
        self._slept_through = None

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

            tasks.start_soon(self._while_the_link_holds, read_keyboard)
            tasks.start_soon(self._while_the_link_holds, self._watch_signal)
            tasks.start_soon(self._while_the_link_holds, self._watch_size)
            tasks.start_soon(self._while_the_link_holds, self._reap_forwards)
            # It writes no packet, so it needs no guard against a link
            # that stopped taking them: ending the link is its whole job.
            tasks.start_soon(self._watch_for_suspend, connection)

            try:
                # Inside the try, because the link can go between the
                # connection and the first packet. A failure there is
                # the same failure, and it has to reach the retries
                # rather than leave the terminal on the other screen.
                self._start_gui(detach_other_clients, color_depth)
                # What the table holds after `_connect` opened it
                # again. The server's copy is drawn from this, and it
                # has to arrive after `start-gui`, because until then
                # the connection has no client to hang it on.
                #
                # It carries a message only when something moved, so a
                # first attach and a reconnect that changed nothing are
                # both silent. Lillecarl/pymux#442.
                self._report_forwards(self._forwards_moved)
                self._forwards_moved = ""

                async for packet in self._packets(reader):
                    if packet[Field.CMD] == Packet.FORWARD:
                        # Opening one is a coroutine and `_process` is
                        # not, so it goes to the scope that owns this
                        # attachment rather than blocking the reader.
                        tasks.start_soon(
                            self._while_the_link_holds,
                            self._forward_asked,
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
                            self._open_asked,
                            connection,
                            packet,
                        )
                        continue

                    self._process(json.dumps(packet).encode("utf-8"))
            except Exception as error:
                lost = error
            finally:
                # The three readers above end with this scope.
                tasks.cancel_scope.cancel()
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self._set_kitty_flags(0)
                self._restore_modes()
                self._writer = None
                # The listeners are this table's own, so nothing else
                # closes them. Left open they would accept a browser
                # and then carry nothing, which is a worse answer than
                # a refused connection while the link is down. The
                # wanted set stays, and `_connect` opens it again.
                self.forwards.close()
                connection.close()

        if lost is None and self._slept_through is not None:
            # The link ended cleanly because this client ended it. Left
            # alone that reads as a detach, and the panes on the other
            # machine would be abandoned by the one thing that was
            # supposed to go back to them. Lillecarl/pymux#445.
            lost = self._slept_through

        return lost

    # ------------------------------------------------------------------
    # Forwarded ports.

    async def _forward_asked(self, connection, packet) -> None:
        """
        Add or remove one forward, because the server asked.

        **The server asks and this side acts**, the way it does for a
        URL to open: only the machine a person sits at can bind their
        port, and only this client holds the SSH connection that
        carries it. Lillecarl/pymux#436, Lillecarl/pymux#261.

        The answer is the whole table and not the one that changed. It
        is a handful of lines, it makes the server's copy right after
        a reconnect as well as after a change, and a person reading
        `list-forwards` wants the set anyway.
        """
        if packet.get(Field.REMOVE):
            where = (
                Direction(packet[Field.DIRECTION]),
                packet[Field.LISTEN_HOST],
                packet[Field.LISTEN_PORT],
            )
            gone = self.forwards.remove(where)

            if gone is None:
                said = "Nothing was forwarding %s:%s." % (
                    packet[Field.LISTEN_HOST],
                    packet[Field.LISTEN_PORT],
                )
            else:
                said = "Stopped forwarding %s." % (gone.spell(),)

            self._report_forwards(said)
            return

        forward = Forward(
            direction=Direction(packet[Field.DIRECTION]),
            listen_host=packet[Field.LISTEN_HOST],
            listen_port=packet[Field.LISTEN_PORT],
            dest_host=packet[Field.DEST_HOST],
            dest_port=packet[Field.DEST_PORT],
        )
        opened = await self.forwards.add(connection, forward)

        if opened.error:
            said = "Cannot forward %s: %s" % (forward.spell(), opened.error)
        elif opened.port != forward.listen_port:
            # The person asked for any free port, so the number is news.
            said = "Forwarding %s:%d to %s." % (
                forward.listen_host,
                opened.port,
                forward.dest,
            )
        else:
            said = "Forwarding %s." % (forward.spell(),)

        if not opened.error and the_far_side_may_narrow(forward):
            # **The listener is open and the address may not be the one
            # asked for.** Saying "Forwarding -R 0.0.0.0:2222" flat
            # would be the client asserting something it cannot know:
            # the reply to `tcpip-forward` carries a port and no
            # address. Lillecarl/pymux#444.
            said = "%s Asked for %s, but %s." % (
                said,
                forward.listen_host,
                MAY_NARROW,
            )

        self._report_forwards(said)

    async def _open_asked(self, connection, packet) -> None:
        """
        Forward the port a URL names, then open the URL.

        **The port first, and the page after it.** A browser that
        arrives before the listener does gets a refused connection and
        an error page, and reloading is then the person's job.

        The URL moves when its own port is taken here. Nothing else can
        say so: the server named the address that a pane printed, and
        by the time this knows the real number the packet has already
        been sent. So the message about it comes from this side.
        Lillecarl/pymux#437.
        """
        url = packet[Field.DATA]
        asked = packet[Field.FORWARD]
        host, port = asked["host"], asked["port"]

        opened = await self.forwards.add(
            connection,
            Forward(Direction.LOCAL, host, port, host, port),
            idle=asked["idle"],
        )

        if opened.error:
            said = "Opened %s, but not %s:%d: %s" % (
                url,
                host,
                port,
                opened.error,
            )
        else:
            if opened.port != port:
                url = with_port(url, opened.port)
            said = "Opened %s in the browser of this machine." % (url,)

        reached = self._open_url(url)
        self._report_forwards(said if reached else "")

        if not reached:
            self._send_packet({Field.CMD: Packet.OPEN_FAILED, Field.DATA: url})

    async def _reap_forwards(self) -> None:
        "Give back the port of a URL that nobody is using any more."
        while True:
            await anyio.sleep(REAP_POLL)

            gone = self.forwards.reap()
            if gone:
                self._report_forwards(
                    "Stopped forwarding %s: nothing used it." % ("; ".join(one.listen for one in gone),)
                )

    def _report_forwards(self, message: str = "") -> None:
        """
        Tell the server what this client is forwarding.

        The server keeps a copy to draw and never the truth: it has no
        SSH connection, so it cannot know whether a listener is really
        open. `list-forwards` reads the copy.
        """
        self._send_packet(
            {
                Field.CMD: Packet.FORWARDS,
                Field.DATA: self.forwards.report(),
                Field.MESSAGE: message,
            }
        )

    async def _while_the_link_holds(self, work, *arguments) -> None:
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

    def _clocks(self) -> tuple[float, float]:
        """
        The world's clock and the loop's, read together.

        A method so that a test can move one of them without sleeping
        a machine.
        """
        return time.time(), anyio.current_time()

    async def _watch_for_suspend(self, connection) -> None:
        """
        End the link when this machine has been asleep.

        **A sleep is invisible to everything else here.** The loop's
        clock is `time.monotonic`, which on Linux does not run while
        the machine is suspended, so every timer that would notice a
        dead link is suspended with it: asyncssh's keepalive resumes
        counting from where it stopped and takes its full four
        intervals *after* the lid opens. The person looks at a frozen
        screen for that whole time, and the panes they are looking at
        have been reachable for most of it.

        So this watches the one thing a sleep cannot hide: the world
        moved and the loop did not. It needs no signal, no bus and no
        platform code, and it is right whichever way a platform
        chooses. Where the loop's clock keeps running through a sleep
        there is no gap to find, and there is nothing to fix either --
        the keepalive timers ran too, so they have already given up by
        the time anybody looks.

        **A gap ends the link rather than testing it.** A machine that
        has been asleep has almost always lost its TCP connections,
        and the two outcomes are not equal: reconnecting a link that
        was alive costs about a second and the panes are on the other
        machine anyway, while waiting on a link that is dead costs the
        twenty seconds the keepalive needs. A clock that was stepped
        forwards by something other than a sleep pays the same second.

        **A link this client ended reads as end of file either way**,
        and end of file is how a server says the person detached. So
        the reason is written down before the link goes, and
        `_attached` uses it in place of the ending it would otherwise
        read. `_packets` measured `ConnectionLost` for an abort, but
        that was the far side aborting; aborting our own gives a clean
        end here, and a client that trusted the measurement left
        instead of coming back.

        A reset is what the reason says, because that is what a sleep
        did to the socket, and `link_may_come_back` retries an
        `OSError`. Lillecarl/pymux#445.
        """
        was_wall, was_loop = self._clocks()

        while True:
            await anyio.sleep(SUSPEND_POLL)

            wall, loop = self._clocks()
            asleep = (wall - was_wall) - (loop - was_loop)
            was_wall, was_loop = wall, loop

            if asleep >= SUSPEND_GAP:
                self._slept_through = ConnectionResetError("This machine was asleep for %s." % (_how_long(asleep),))
                connection.abort()
                return

    async def _watch_signal(self) -> None:
        "Report the size when the terminal says it changed."
        try:
            with anyio.open_signal_receiver(signal.SIGWINCH) as signals:
                async for _signum in signals:
                    self._send_size()
        except NotImplementedError, ValueError, RuntimeError:
            pass  # No signals here. The size stays as it was.

    # ------------------------------------------------------------------
    # The link, when it goes.

    async def _link_again(self, stdin_fd: int, waits: Backoff, lost):
        """
        Show that the server is gone, and open the link again.

        The connection and its reader, or `None` when the person
        stopped it. A failure that never comes back is raised, because
        there is nothing left for this client to do about it.

        **The screen keeps saying what is happening.** A probe runs
        beside the retries, so the notice can say whether this machine
        can reach the internet at all, and each attempt is drawn while
        it is in flight, because an attempt can outlast its wait by
        far. `reconnect.notice` holds the lines.
        """
        output = Vt100_Output.from_pty(sys.stdout)
        internet = Internet()

        try:
            async with anyio.create_task_group() as watching:
                watching.start_soon(watch_internet, internet, WAN_INTERVAL)

                while True:
                    if not link_may_come_back(lost):
                        watching.cancel_scope.cancel()
                        raise lost

                    outcome = await self._one_try(output, stdin_fd, waits.next(), lost, internet)

                    if isinstance(outcome, _Left):
                        watching.cancel_scope.cancel()
                        return None
                    if isinstance(outcome, Exception):
                        lost = outcome
                        continue

                    watching.cancel_scope.cancel()
                    return outcome
        finally:
            output.show_cursor()
            output.flush()

    async def _one_try(self, output, stdin_fd: int, wait: float, lost, internet):
        """
        Count down one wait, then attempt the link.

        `_LEFT`, the failure, or the connection. Any key but q ends the
        wait at once, which is what somebody does who knows the link
        is back.
        """
        ends_at = anyio.current_time() + wait

        while True:
            left = ends_at - anyio.current_time()
            if left <= 0:
                break

            self._draw(output, lost, internet, left)
            with anyio.move_on_after(min(left, COUNTDOWN_STEP)) as when:
                await anyio.wait_readable(stdin_fd)
            if when.cancelled_caught:
                continue  # Nothing typed. Draw the countdown again.

            typed = self._read_typed(stdin_fd)
            if typed is None or any(one in typed for one in LEAVE):
                return _LEFT  # No keyboard, or the person is done.
            if typed:
                break  # Somebody who knows the link is back already.

        return await self._attempt(output, stdin_fd, lost, internet)

    def _draw(self, output, lost, internet, seconds: float, trying: bool = False) -> None:
        "Put the notice of this moment on the terminal."
        rows, columns = self.size()
        draw(
            output,
            rows,
            columns,
            notice(self.target.host, why(lost), seconds, internet.value, trying),
        )

    def _read_typed(self, stdin_fd: int) -> str | None:
        "What is typed right now, or None when the keyboard is gone."
        with nonblocking(stdin_fd):
            typed = self._stdin_reader.read()
        if self._stdin_reader.closed:
            return None
        return typed

    async def _attempt(self, output, stdin_fd: int, lost, internet):
        """
        Open the link while the screen keeps saying it is trying.

        The connection, the failure, or `_LEFT`. The attempt runs as a
        task of its own, so this one can draw and listen beside it;
        cancelling the scope is how the winner ends the other.
        """
        link = None
        failure: Exception | None = None
        left = False

        async with anyio.create_task_group() as tasks:

            async def connect() -> None:
                nonlocal link, failure
                try:
                    link = await self._connect(CONNECT_TIMEOUT)
                except Exception as error:
                    failure = error
                finally:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(connect)

            while True:
                self._draw(output, lost, internet, 0.0, trying=True)
                with anyio.move_on_after(COUNTDOWN_STEP) as when:
                    await anyio.wait_readable(stdin_fd)
                if when.cancelled_caught:
                    continue

                typed = self._read_typed(stdin_fd)
                if typed is None or any(one in typed for one in LEAVE):
                    left = True
                    tasks.cancel_scope.cancel()
                    break

        if left:
            return _LEFT
        if failure is not None:
            return failure
        return link

    # ------------------------------------------------------------------

    async def _packets(self, reader):
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
        framer = Framer()

        while True:
            data = await reader.read(4096)

            if not data:
                return

            for one in framer.feed(data):
                yield json.loads(one.decode("utf-8"))

    async def _watch_size(self) -> None:
        """
        Tell the server whenever the terminal has a new size.

        The signal handler above does this at once. This is what covers
        a terminal that sends no signal, and what `client/memory.py`
        needs for a reason of its own.
        """
        last = self.size()

        while True:
            await anyio.sleep(SIZE_INTERVAL)

            size = self.size()
            if size != last:
                last = size
                self._send_size()
