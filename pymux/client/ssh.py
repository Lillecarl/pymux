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

**Nothing runs on the other machine.** Not a shell, not a pymux, not
even to find out which socket to open: `ssh://host` with no path lists
the sockets over SFTP, which is a subsystem of sshd itself. So the far
side needs a pymux server and an sshd, and nothing else.

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

import json
import signal
import sys
from typing import NamedTuple
from urllib.parse import urlparse

import anyio

from prompt_toolkit.input.vt100 import raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.utils import nonblocking

from .reconnect import Backoff, draw, link_may_come_back, notice, why
from .terminal import TerminalClient

__all__ = [
    "SshClient",
    "SshTarget",
    "is_ssh_url",
    "ssh_target",
]

#: What `-S` starts with when it names a machine rather than a path.
SCHEME = "ssh://"

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
KEEPALIVE_INTERVAL = 15
KEEPALIVE_MISSES = 3

#: How often the disconnected screen draws its countdown, in seconds.
COUNTDOWN_STEP = 1.0

#: What a person types to stop trying.
LEAVE = ("q", "Q", "\x03")


#: Where a server binds when nobody named a socket, and the shape of
#: the name it takes. `pipes/posix.py` builds it, and
#: `client/posix.py` globs the same thing to list the servers here.
#:
#: `/tmp` and not `tempfile.gettempdir()`, because the directory
#: belongs to the other machine. It is what `TMPDIR` unset means, which
#: is what a login shell almost always has.
SOCKET_DIRECTORY = "/tmp"
SOCKET_NAMES = "pymux.sock.%s.*"


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


def is_ssh_url(name: str | None) -> bool:
    "Whether this `-S` names a machine."
    return bool(name) and str(name).startswith(SCHEME)


def default_socket(username: str) -> str:
    """
    Where the first server of a user listens.

    The fallback, for a machine whose sshd does not offer SFTP. A
    server with no name takes the lowest free number, so the first one
    is always `.0`, and most machines have exactly one.
    `SshClient._socket` is what asks rather than guesses.
    """
    return "%s/pymux.sock.%s.0" % (SOCKET_DIRECTORY, username)


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
        self._writer = None

    # ------------------------------------------------------------------
    # The transport.

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

    async def _connect(self):
        """
        Open the channel to the socket on the far side.

        Returns the connection, so that the caller closes it. It is a
        context manager, and asyncssh closes the channel with it.
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
        return connection, reader

    async def _socket(self, connection) -> str:
        """
        Which socket to open, when the address named none.

        **Nothing runs on the other machine.** The listing goes over
        SFTP, which is a subsystem of sshd itself and not a program
        anybody has to install; the channel to the socket is
        `direct-streamlocal@openssh.com`, which is sshd as well. So a
        remote machine needs a pymux server and an sshd, and nothing
        else at all.

        The newest server, which is what `pymux attach` with no `-S`
        means on this machine: `client/posix.py` sorts the same names
        by the same time, because nothing writes to a socket file after
        the bind, so its time is the time the server started.

        The user is the one that was really authenticated, which is
        better than a guess here: `~/.ssh/config` can name a different
        one, and asyncssh has already applied it.
        """
        import stat

        username = connection.get_extra_info("username")
        pattern = "%s/%s" % (
            SOCKET_DIRECTORY,
            SOCKET_NAMES % (username,),
        )

        try:
            async with connection.start_sftp_client() as sftp:
                found = await sftp.glob_sftpname(pattern)
        except Exception:
            # No SFTP subsystem, or nothing matched. Fall back to where
            # the first server of a user listens, which is right on a
            # machine that has one.
            return default_socket(username)

        sockets = [
            one
            for one in found
            if one.attrs.permissions and stat.S_ISSOCK(one.attrs.permissions)
        ]
        if not sockets:
            return default_socket(username)

        newest = max(sockets, key=lambda one: one.attrs.mtime or 0)
        name = newest.filename
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")

        # `glob` answers with the path it was given, which was absolute.
        return (
            name
            if name.startswith("/")
            else "%s/%s"
            % (
                SOCKET_DIRECTORY,
                name,
            )
        )

    # ------------------------------------------------------------------
    # What a person runs.

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
                {"cmd": "run-command", "data": command, "pane_id": pane_id}
            )

            exit_code = 0
            try:
                async for packet in self._packets(reader):
                    if packet["cmd"] == "out":
                        sys.stdout.write(packet["data"])
                        sys.stdout.flush()
                    elif packet["cmd"] == "err":
                        sys.stderr.write(packet["data"])
                        sys.stderr.flush()
                    elif packet["cmd"] == "exit":
                        exit_code = packet["code"]
            except Exception as error:
                # The link went before the answer arrived. Nothing here
                # knows whether the command ran, so the code has to say
                # that it does not.
                sys.stderr.write(
                    "pymux lost the server on %s: %s\n" % (self.target.host, why(error))
                )
                return 1
            return exit_code
        finally:
            connection.close()

    def attach(self, detach_other_clients: bool = False, color_depth=None) -> None:
        anyio.run(self._attach, detach_other_clients, color_depth)

    async def _attach(
        self, detach_other_clients: bool = False, color_depth=None
    ) -> None:
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
                lost = await self._attached(
                    connection, reader, stdin_fd, detach_others, color_depth
                )
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

                try:
                    again = await self._link_again(stdin_fd, waits, lost)
                except Exception as error:
                    cannot = error
                    break

                if again is None:
                    break  # The person stopped it.

                connection, reader = again
                waits.reset()

        self._reset_terminal()

        if cannot is not None:
            sys.stderr.write(
                "pymux cannot reach the server on %s: %s\n"
                % (self.target.host, why(cannot))
            )
            self.exit_code = 1

    async def _attached(
        self, connection, reader, stdin_fd, detach_other_clients, color_depth
    ):
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

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self._while_the_link_holds, self._read_keyboard, stdin_fd)
            tasks.start_soon(self._while_the_link_holds, self._watch_signal)
            tasks.start_soon(self._while_the_link_holds, self._watch_size)

            try:
                # Inside the try, because the link can go between the
                # connection and the first packet. A failure there is
                # the same failure, and it has to reach the retries
                # rather than leave the terminal on the other screen.
                self._start_gui(detach_other_clients, color_depth)

                async for packet in self._packets(reader):
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
                connection.close()

        return lost

    async def _while_the_link_holds(self, work, *arguments) -> None:
        """
        Run one task of an attachment, and end it quietly when the link
        stops taking packets.

        All three of them write, and the link can go between any two
        writes. Without this the one that loses the race raises out of
        the task group, which is not a fault: the read loop has seen
        the same end and is about to cancel these anyway.
        """
        try:
            await work(*arguments)
        except BrokenPipeError:
            pass

    async def _read_keyboard(self, stdin_fd: int) -> None:
        "Give the server what the person types, until this is cancelled."
        while True:
            await anyio.wait_readable(stdin_fd)
            self._process_stdin()

    async def _watch_signal(self) -> None:
        "Report the size when the terminal says it changed."
        try:
            with anyio.open_signal_receiver(signal.SIGWINCH) as signals:
                async for _signum in signals:
                    self._send_size()
        except (NotImplementedError, ValueError, RuntimeError):
            pass  # No signals here. The size stays as it was.

    # ------------------------------------------------------------------
    # The link, when it goes.

    async def _link_again(self, stdin_fd: int, waits: Backoff, lost):
        """
        Show that the server is gone, and open the link again.

        The connection and its reader, or `None` when the person
        stopped it. A failure that never comes back is raised, because
        there is nothing left for this client to do about it.
        """
        output = Vt100_Output.from_pty(sys.stdout)

        try:
            while True:
                if not link_may_come_back(lost):
                    raise lost

                if not await self._count_down(output, stdin_fd, waits.next(), lost):
                    return None

                try:
                    return await self._connect()
                except Exception as error:
                    lost = error
        finally:
            output.show_cursor()
            output.flush()

    async def _count_down(self, output, stdin_fd: int, wait: float, lost) -> bool:
        """
        Draw the disconnected screen until the wait is over.

        False when the person stopped it. Any other key ends the wait
        at once, which is what somebody does who knows the link is
        back.
        """
        ends_at = anyio.current_time() + wait

        while True:
            left = ends_at - anyio.current_time()
            if left <= 0:
                return True

            rows, columns = self.size()
            draw(output, rows, columns, notice(self.target.host, why(lost), left))

            with anyio.move_on_after(min(left, COUNTDOWN_STEP)) as when:
                await anyio.wait_readable(stdin_fd)

            if when.cancelled_caught:
                continue  # Nothing typed. Draw the countdown again.

            with nonblocking(stdin_fd):
                typed = self._stdin_reader.read()

            if self._stdin_reader.closed:
                return False  # No keyboard left to answer with.
            if any(one in typed for one in LEAVE):
                return False
            if typed:
                return True

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
        held = b""

        while True:
            data = await reader.read(4096)

            if not data:
                return

            held += data
            while b"\0" in held:
                one, held = held.split(b"\0", 1)
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
