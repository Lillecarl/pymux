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
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import NamedTuple
from urllib.parse import urlparse

from prompt_toolkit.input.vt100 import raw_mode

from .terminal import TerminalClient

__all__ = [
    "SshClient",
    "SshTarget",
    "is_an_ssh_url",
    "the_ssh_target",
]

#: What `-S` starts with when it names a machine rather than a path.
THE_SCHEME = "ssh://"

#: How often to tell the server the terminal has a new size, in
#: seconds. `client/memory.py` says why a poll and not only a signal.
SIZE_INTERVAL = 0.5


#: Where a server binds when nobody named a socket, and the shape of
#: the name it takes. `pipes/posix.py` builds it, and
#: `client/posix.py` globs the same thing to list the servers here.
#:
#: `/tmp` and not `tempfile.gettempdir()`, because the directory
#: belongs to the other machine. It is what `TMPDIR` unset means, which
#: is what a login shell almost always has.
THE_SOCKET_DIRECTORY = "/tmp"
THE_SOCKET_NAMES = "pymux.sock.%s.*"


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


def is_an_ssh_url(name: str | None) -> bool:
    "Whether this `-S` names a machine."
    return bool(name) and str(name).startswith(THE_SCHEME)


def the_default_socket(username: str) -> str:
    """
    Where the first server of a user listens.

    The fallback, for a machine whose sshd does not offer SFTP. A
    server with no name takes the lowest free number, so the first one
    is always `.0`, and most machines have exactly one.
    `SshClient._the_socket` is what asks rather than guesses.
    """
    return "%s/pymux.sock.%s.0" % (THE_SOCKET_DIRECTORY, username)


def the_ssh_target(url: str) -> SshTarget:
    """
    Read `ssh://[user@]host[:port][/path/to/socket]`.

    With no path the socket is found after connecting, so the path is
    `None` here and `SshClient._the_socket` fills it in.
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
        self.target = the_ssh_target(socket_name)
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
        self._writer.write(json.dumps(data).encode("utf-8") + b"\0")

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
        asking = dict(self.connect_with)
        if target.port is not None:
            asking.setdefault("port", target.port)
        if target.username is not None:
            asking.setdefault("username", target.username)

        connection = await asyncssh.connect(target.host, **asking)
        try:
            path = target.path or await self._the_socket(connection)
            reader, writer = await connection.open_unix_connection(path)
        except Exception:
            connection.close()
            raise

        self.path = path
        self._writer = writer
        return connection, reader

    async def _the_socket(self, connection) -> str:
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
        import stat as the_stat

        username = connection.get_extra_info("username")
        pattern = "%s/%s" % (
            THE_SOCKET_DIRECTORY,
            THE_SOCKET_NAMES % (username,),
        )

        try:
            async with connection.start_sftp_client() as sftp:
                found = await sftp.glob_sftpname(pattern)
        except Exception:
            # No SFTP subsystem, or nothing matched. Fall back to where
            # the first server of a user listens, which is right on a
            # machine that has one.
            return the_default_socket(username)

        sockets = [
            one
            for one in found
            if one.attrs.permissions and the_stat.S_ISSOCK(one.attrs.permissions)
        ]
        if not sockets:
            return the_default_socket(username)

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
                THE_SOCKET_DIRECTORY,
                name,
            )
        )

    # ------------------------------------------------------------------
    # What a person runs.

    def run_command(self, command, pane_id=None) -> int:
        return asyncio.run(self._run_command(command, pane_id))

    async def _run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command, print what it says, and
        return the exit code. `client/posix.py` reads the same packets.
        """
        connection, reader = await self._connect()

        try:
            self._send_packet(
                {"cmd": "run-command", "data": command, "pane_id": pane_id}
            )

            exit_code = 0
            async for packet in self._packets(reader):
                if packet["cmd"] == "out":
                    sys.stdout.write(packet["data"])
                    sys.stdout.flush()
                elif packet["cmd"] == "err":
                    sys.stderr.write(packet["data"])
                    sys.stderr.flush()
                elif packet["cmd"] == "exit":
                    exit_code = packet["code"]
            return exit_code
        finally:
            connection.close()

    def attach(self, detach_other_clients: bool = False, color_depth=None) -> None:
        asyncio.run(self._attach(detach_other_clients, color_depth))

    async def _attach(
        self, detach_other_clients: bool = False, color_depth=None
    ) -> None:
        """
        Attach the user interface, and return when the server closes
        the connection.

        The loop is the same shape as `client/memory.py`: the keyboard
        is read through the loop rather than through `select`, because
        the packets are awaited and one thread cannot do both.
        """
        loop = asyncio.get_running_loop()
        stdin_fd = sys.stdin.fileno()

        connection, reader = await self._connect()
        self._start_gui(detach_other_clients, color_depth)

        with raw_mode(stdin_fd):
            loop.add_reader(stdin_fd, self._process_stdin)
            try:
                loop.add_signal_handler(signal.SIGWINCH, self._send_size)
            except (NotImplementedError, ValueError):
                pass  # No signals here. The size stays as it was.

            # Held, so that nothing collects it while it waits.
            watcher = loop.create_task(self._watch_the_size())

            try:
                async for packet in self._packets(reader):
                    self._process(json.dumps(packet).encode("utf-8"))
                # The server closed the connection. Put the terminal of
                # the user back as it was.
                self._reset_terminal()
            finally:
                watcher.cancel()
                loop.remove_reader(stdin_fd)
                try:
                    loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, ValueError):
                    pass
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self._set_kitty_flags(0)
                connection.close()

    # ------------------------------------------------------------------

    async def _packets(self, reader):
        """
        The packets of the server, one at a time.

        A packet ends at a zero byte and a read can hold any part of
        one, so the tail of a read is the head of the next packet.
        `client/posix.py` splits the same stream the same way.
        """
        held = b""

        while True:
            try:
                data = await reader.read(4096)
            except Exception:
                # The connection went. Same as the end of the stream.
                return

            if not data:
                return

            held += data
            while b"\0" in held:
                one, held = held.split(b"\0", 1)
                yield json.loads(one.decode("utf-8"))

    async def _watch_the_size(self) -> None:
        """
        Tell the server whenever the terminal has a new size.

        The signal handler above does this at once. This is what covers
        a terminal that sends no signal, and what `client/memory.py`
        needs for a reason of its own.
        """
        last = self.the_size()

        while True:
            await asyncio.sleep(SIZE_INTERVAL)

            size = self.the_size()
            if size != last:
                last = size
                self._send_size()
