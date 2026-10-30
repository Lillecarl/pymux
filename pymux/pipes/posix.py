from __future__ import annotations

import contextlib
import errno
import fcntl
import getpass
import os
import socket
import stat
from collections.abc import Callable
from typing import override

import anyio
from libpymux.protocol import READ_SIZE, Message
from libpymux.sockets import nobody_answers, socket_directory
from libpymux.wire import Compressor, PacketReader

from ..log import logger
from .base import BrokenPipeError, PipeConnection, Turns

__all__ = [
    "PosixSocketConnection",
    "PosixSocketListener",
    "bind_and_listen_on_posix_socket",
]


#: The clients that may wait to be taken. tmux's number.
#:
#: **An upgrade takes no client for about a second**, from the pause to
#: the new image's first accept, and every client comes back in that
#: second. At 0 the kernel queued one: the rest were refused, and an
#: SSH client heard "open failed" and left. Lillecarl/pymux#750.
BACKLOG = 128


def bind_and_listen_on_posix_socket(
    socket_name: str | None, accept_callback: Callable[[PipeConnection], None]
) -> PosixSocketListener:
    """
    Bind a unix socket, and answer with the listener that serves it.

    **The bind and the accepting are two steps, and they happen in two
    places.** A server binds before `daemonize` forks, because the name
    is what the client that started it connects to; it accepts in the
    task group of `Pymux.running`, which exists only once the loop
    turns. Lillecarl/pymux#87.

    :param accept_callback: Called with `PosixSocketConnection` when a
        new connection is established.
    """
    # Set umask for the socket file.
    #
    # **Put back even when the bind fails.** The umask belongs to the
    # process, so a bind that raised and left it would put every file
    # this process writes afterwards at 0027. A suite that expects a
    # directory it makes to be group readable is where that shows.
    old_umask = os.umask(int("0027", 8))
    try:
        socket_name, sock = _bind_posix_socket(socket_name)
    finally:
        _ = os.umask(old_umask)

    sock.listen(BACKLOG)

    logger.info("Listening on %r." % socket_name)
    return PosixSocketListener(socket_name, sock, accept_callback)


class PosixSocketListener:
    "A bound socket, and the coroutine that takes the clients of it."

    def __init__(
        self, socket_name: str, sock: socket.socket, accept_callback: Callable[[PipeConnection], None]
    ) -> None:
        self.socket_name = socket_name
        self.socket = sock
        self._accept_callback = accept_callback
        #: Set while clients are taken. `pause` leaves them waiting in the
        #: backlog, for whoever takes clients on this socket next.
        self._taking = anyio.Event()
        self._taking.set()

    def pause(self) -> None:
        "Take no client until `resume`. An upgrade's new server takes them instead."
        self._taking = anyio.Event()

    def resume(self) -> None:
        self._taking.set()

    async def serve(self) -> None:
        "Take every client that connects, until this task is cancelled."
        while True:
            await self._taking.wait()
            try:
                await anyio.wait_readable(self.socket)
            except anyio.ClosedResourceError:
                return  # `close` stopped the listening.
            if not self._taking.is_set():
                continue  # paused while it waited: the client is not this server's

            connection, _client_address = self.socket.accept()
            # The socket goes to `PosixSocketConnection` non blocking,
            # and every send waits for room before it retries. A client
            # that stopped reading must not park the loop inside a
            # send: the partial-send loop in `_send_all` is also what
            # keeps a packet whole on OS X, where a non blocking send
            # of more than the buffer holds sends part of it.
            # Lillecarl/pymux#418.
            self._accept_callback(PosixSocketConnection(connection))

    def close(self) -> None:
        """
        Stop taking clients. A client that connects after this is
        refused at once: a restart closes the listening first, so a
        waiting client never reaches the server that is going.
        Lillecarl/pymux#409.
        """
        with contextlib.suppress(OSError, RuntimeError):
            # Wakes `serve` with `ClosedResourceError`. Outside the loop
            # there is no waiter to wake.
            anyio.notify_closing(self.socket)
        with contextlib.suppress(OSError):
            self.socket.close()


def _bind_or_take_over(sock: socket.socket, socket_name: str) -> None:
    """
    Bind the name, taking it from a server that has gone.

    **A killed server leaves its socket file behind**, and a bind on
    that name answers EADDRINUSE for as long as the file is there --
    for ever, because nothing takes it away. So `pymux -S <path>
    new-session` could not start a server on the path its own last
    server had used. tmux answers this by unlinking a socket that
    nobody is listening on, and this is that answer.

    **The lock is what makes the unlink safe.** Between the question
    "does anybody answer" and the bind, another process may start a
    server on the same name, and unlinking then would take the name
    away from a server that is alive. So the question is asked again
    under `<path>.lock`, and the lock is held until this socket is
    bound. tmux holds the same lock over the same two steps, and its
    comment says why it re-asks even when the lock was free.

    A name that something answers on stays where it is: the caller
    sees the EADDRINUSE it would have seen. So does a name that holds
    anything but a socket -- a file somebody else put there is not
    ours to delete. Lillecarl/pymux#453.
    """
    try:
        sock.bind(socket_name)
        return
    except OSError as busy:
        if busy.errno != errno.EADDRINUSE:
            raise
        in_use = busy

    # **`O_NOFOLLOW`, because the lock file is a name beside the socket
    # and a name is something another account can get there first.**
    # `os.open` follows a symlink, so for `pymux -S /tmp/shared.sock`
    # somebody else could make `/tmp/shared.sock.lock` point at a file
    # of their choosing and pymux would flock that -- and hold a server
    # start for as long as it liked. Nothing is written and there is no
    # `O_TRUNC`, so no content was ever at risk; the lock was.
    #
    # It costs nothing honest: nobody symlinks their own lock file, and
    # a real one still opens. A symlink answers ELOOP, which the
    # explicit path reports and the room's loop reads as a name to step
    # past. The per-UID room is 0700 so only an explicitly named socket
    # in a directory somebody else can write is exposed at all, which
    # is the shape of Lillecarl/pymux#405 again. Lillecarl/pymux#455.
    lock = os.open(
        socket_name + ".lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)

        if not nobody_answers(socket_name):
            raise in_use

        try:
            left_behind = os.lstat(socket_name)
        except FileNotFoundError:
            # Somebody took it away while this waited for the lock, so
            # there is nothing to take and the bind below has the name.
            pass
        else:
            if not stat.S_ISSOCK(left_behind.st_mode):
                raise in_use
            os.unlink(socket_name)
            logger.info("Took %r from a server that has gone.", socket_name)

        sock.bind(socket_name)
    finally:
        os.close(lock)


def _bind_posix_socket(socket_name: str | None = None):
    """
    Find a socket to listen on and return it.

    Returns (socket_name, sock_obj)
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    if socket_name:
        # Absolute, always. The name this returns is the one the server
        # keeps, and it outlives the directory it was typed in: the bind
        # happens before `daemonize`, which does `os.chdir("/")`, so a
        # relative name means one thing here and another everywhere after.
        #
        # Two things went wrong with `pymux -S pymux.sock`. The server
        # could not take its own socket file away when it stopped --
        # `os.remove` looked for it under `/` and the `except OSError`
        # there swallowed the miss, so the file stayed. And a pane was
        # given `PYMUX=pymux.sock,%1`, so a program inside it could not
        # find the server it runs in unless it happened to share the
        # directory pymux was started from. tmux writes `TMUX` absolute
        # for that reason. Lillecarl/pymux#322.
        socket_name = os.path.abspath(socket_name)
        _bind_or_take_over(s, socket_name)
        return socket_name, s
    room = socket_directory()
    i = 0
    while True:
        try:
            socket_name = "%s/pymux.sock.%s.%i" % (
                room,
                getpass.getuser(),
                i,
            )
            # **A dead name is taken and not stepped over.** A
            # killed server leaves its file behind and nothing ever
            # took one away, so the numbers only ever went up: a
            # person read a bigger one in `PYMUX` every time, and
            # after a hundred of them no server could start at all.
            # The explicit path has answered this since
            # Lillecarl/pymux#453 and the room gets the same answer.
            # A name something answers on raises EADDRINUSE here,
            # which is the next index. Lillecarl/pymux#454.
            _bind_or_take_over(s, socket_name)
            return socket_name, s
        except OSError:
            i += 1

            # When 100 times failed, cancel server
            if i == 100:
                logger.warning("100 times failed to listen on posix socket. Please clean up old sockets.")
                raise


#: What may wait to be read by one client before it is left behind.
#:
#: The kernel holds about 200 kB on a unix socket before a send has to
#: wait for room. Past a megabyte the client is not reading slowly, it
#: is not reading at all -- suspended with ctrl+z, a laptop asleep --
#: and an animating pane would pile up one parked write after another
#: for as long as the server runs. tmux leaves such a client behind
#: too ("client is too slow"): the person who comes back reads an
#: ended attachment, and the server goes on. Lillecarl/pymux#418.
TOO_SLOW_BYTES = 1024 * 1024

#: A packet this big or bigger is compressed in a worker thread. zstd
#: lets go of the GIL while it works, and a window pane's raw frame of
#: 5.6 MB takes 5 to 9 ms at level 1. A smaller packet costs less on the
#: loop than the trip to a thread. Lillecarl/pymux#632.
COMPRESS_IN_A_THREAD = 64 * 1024

#: How long a closed connection reads on, for the far end to read what
#: it was sent and close its side too.
LINGER_SECONDS = 2.0


class PosixSocketConnection(PipeConnection):
    """
    A single active posix pipe connection on the server side.
    """

    #: The socket can be the far end of an ssh channel.
    can_compress = True

    def __init__(self, socket: socket.socket) -> None:
        self.socket = socket
        # Non blocking. A send that finds no room parks this
        # connection's write and never the loop (`write`).
        # Lillecarl/pymux#418.
        self.socket.setblocking(False)
        self._reader = PacketReader()
        self._ready: list[Message] = []
        self._closed = False

        #: Bytes given to `write` and not yet taken by the kernel.
        self._outstanding = 0
        #: One write at a time, sound first. Lillecarl/pymux#657.
        self._turns = Turns()
        #: Set while `_outstanding` is zero; waited on by `drained`.
        self._empty: anyio.Event | None = None
        #: The zstd stream every write goes into, once one turned it.
        self._compressor: Compressor | None = None
        self.bytes_out = 0
        self.bytes_in = 0

    @property
    @override
    def outstanding(self) -> int:
        return self._outstanding

    @override
    async def drained(self) -> None:
        while self._outstanding and not self._closed:
            if self._empty is None or self._empty.is_set():
                self._empty = anyio.Event()
            await self._empty.wait()

    @override
    async def read(self) -> Message:
        "The next packet, decoded."
        if self._closed:
            raise BrokenPipeError

        while not self._ready:
            self._ready.extend(self._reader.feed(await self._read_chunk()))
        return self._ready.pop(0)

    async def _read_chunk(self) -> bytes:
        "Wait for this socket to say something, and take what it said."
        if self.socket.fileno() == -1:  # Socket closed.
            raise BrokenPipeError

        try:
            await anyio.wait_readable(self.socket)
        except anyio.ClosedResourceError, OSError:
            # `close()` says the socket is going, so that this wakes
            # rather than waiting on a descriptor that is taken away.
            raise BrokenPipeError

        try:
            data = self.socket.recv(READ_SIZE)
        except OSError as e:
            # On OSX, when we try to create a new window by typing "pymux
            # new-window" in a centain pane, very often we get the following
            # error: "OSError: [Errno 9] Bad file descriptor."
            # This doesn't seem very harmful, and we can just try again.
            logger.warning("Got OSError while reading data from client: %s. Trying again.", e)
            return b""

        if not data:
            raise BrokenPipeError

        self.bytes_in += len(data)
        return data

    @override
    async def write(self, frame: bytes, compress_after: bool = False, urgent: bool = False) -> None:
        """
        Write the next encoded packet.

        **A client that stopped reading stops only itself.** The send
        waits for room on its own connection and the loop goes on:
        list-sessions, a later attach and every other client of the
        server keep working while this one holds a full buffer. Past
        `TOO_SLOW_BYTES` the client is left behind. Lillecarl/pymux#418.

        **Compressed under the lock**, so the zstd stream holds the
        frames in the order they go out, which is the order the client
        decompresses them in. Lillecarl/pymux#633.
        """
        if self._closed:
            raise BrokenPipeError

        data = frame

        # Counted before the lock, so that a write waiting for its
        # turn is counted while it waits: the lock serialises the
        # packets (one send, then the next, never interleaved), and
        # this count is what bounds how much they may hold together.
        # Judged on what already waits and not on this packet: a window
        # pane's frame alone can be megabytes. Lillecarl/pymux#629.
        if self._outstanding > TOO_SLOW_BYTES:
            raise BrokenPipeError
        self._outstanding += len(data)

        try:
            async with self._turns.turn(urgent):
                await self._send_all(await self._compressed(data) if self._compressor is not None else data)
                if compress_after:
                    self._compressor = Compressor()
        finally:
            self._outstanding -= len(data)
            if not self._outstanding and self._empty is not None:
                self._empty.set()

    async def _compressed(self, data: bytes) -> bytes:
        """
        `data` in the zstd stream. Called inside the write's turn, so
        the stream takes the packets in the order they go out.
        """
        assert self._compressor is not None
        if len(data) < COMPRESS_IN_A_THREAD:
            return self._compressor.compress(data)
        return await anyio.to_thread.run_sync(self._compressor.compress, data)

    async def _send_all(self, data: bytes) -> None:
        "Send every byte, waiting for room rather than blocking the loop."
        view = memoryview(data)
        while view:
            try:
                sent = self.socket.send(view)
            except BlockingIOError:
                try:
                    await anyio.wait_writable(self.socket)
                except anyio.ClosedResourceError, OSError:
                    # `close()` says the socket is going (it calls
                    # `notify_closing`, which wakes this), or the peer
                    # is gone. Either way this connection is over.
                    raise BrokenPipeError
                continue
            except OSError:
                raise BrokenPipeError
            self.bytes_out += sent
            view = view[sent:]

    @override
    def close(self) -> None:
        """
        Close connection.
        """
        if self._closed:
            return
        self._closed = True
        if self._empty is not None:
            self._empty.set()
        # **Only the sending side shuts here**; `linger` closes the
        # descriptor. A close with the far end's bytes unread fails its
        # next write, and an asyncio peer -- asyncssh's forwarder, in
        # front of an SSH client -- then drops what it had not read yet:
        # the exit packet of a hangup, the notice of a restart.
        # Lillecarl/pymux#710, Lillecarl/pymux#766.
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_WR)
        # Wake whatever is parked on this descriptor. No loop here:
        # nothing is parked on it either.
        with contextlib.suppress(Exception):
            anyio.notify_closing(self.socket)

    @override
    async def linger(self) -> None:
        """
        Read until the far end closes too, for at most `LINGER_SECONDS`,
        then close the descriptor. What arrives now is dropped.
        """
        self.close()
        try:
            with anyio.move_on_after(LINGER_SECONDS):
                while True:
                    await anyio.wait_readable(self.socket)
                    try:
                        if not self.socket.recv(READ_SIZE):
                            break
                    except BlockingIOError:
                        continue
        except anyio.ClosedResourceError, OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                self.socket.close()
