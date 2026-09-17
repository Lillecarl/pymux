import getpass
import os
import socket
import stat
import tempfile
from typing import Callable

import anyio

from ..log import logger
from .base import BrokenPipeError, PipeConnection

__all__ = [
    "bind_and_listen_on_posix_socket",
    "socket_directory",
    "PosixSocketConnection",
    "PosixSocketListener",
]


def socket_directory() -> str:
    """
    The directory that holds this user's unnamed sockets.

    tmux keeps its sockets in `<base>/tmux-<uid>`: a directory it
    creates mode 0700 and then verifies before it uses -- owned by the
    user, closed to everyone else, a real directory and not a symlink.
    A socket named in a flat /tmp is a socket any account can squat in
    front of the bind, and then an attach lands on the squatter.
    Lillecarl/pymux#405.

    The bases, in order: `$PYMUX_TMPDIR`, the override tmux spells
    `$TMUX_TMPDIR`; then the per-user runtime directory the platform
    prefers, which is platformdirs' answer -- it reads
    `$XDG_RUNTIME_DIR` itself where that is the convention, and knows
    `/run/user/<uid>` for a login that never set it, the runtime
    directories of the BSDs, and the room macOS and Windows prefer
    (Lillecarl/pymux#421). No app name is asked for, so the room keeps
    the name `pymux-<uid>` and this move changed no path a running
    server had bound; then what Python names the temp directory, which
    on macOS is the user's own `$TMPDIR`.

    A room that fails the check is a refusal, never a repair: a
    directory somebody else built is exactly the one not to use. The
    refusal falls through to the next base -- a squat must not stop a
    server that a good base would hold -- and the last base's reason
    is what comes out when no base holds a good room.
    """
    bases = []
    value = os.environ.get("PYMUX_TMPDIR")
    # A relative base means another thing after `daemonize` moves
    # the process to /. Lillecarl/pymux#322.
    if value and os.path.isabs(value):
        bases.append(value)
    # Not at the top of the file: a detached command pays the import
    # of nothing it does not use. Lillecarl/pymux#392.
    from platformdirs import PlatformDirs

    # No app name: platformdirs appends one to the base when it is
    # given, and the room below is the per-user folder of this app
    # already -- `pymux-<uid>`, the name tmux spells `tmux-<uid>`.
    bases.append(PlatformDirs().user_runtime_dir)
    bases.append(tempfile.gettempdir())

    failure = None
    for base in bases:
        directory = os.path.join(base, "pymux-%d" % os.getuid())
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        try:
            _verify_the_socket_room(directory)
        except OSError as gone:
            # A room somebody else built is exactly the one not to use,
            # and a squat must not stop a server from starting while a
            # good base stands behind this one.
            logger.warning("Not using %s: %s", directory, gone)
            failure = gone
            continue
        return directory

    if failure is not None:
        # Every room failed its check. The last one is the temp
        # directory, the place the person can look at.
        raise failure
    raise OSError("no base can hold a pymux socket directory")


def _verify_the_socket_room(directory: str) -> None:
    """
    tmux's check, in tmux's order (`tmux.c` `make_label`): a real
    directory -- `lstat`, so a symlink does not pass -- owned by this
    user, with no permission for anybody else.
    """
    room = os.lstat(directory)
    if not stat.S_ISDIR(room.st_mode):
        raise OSError("%s is not a directory" % directory)
    if room.st_uid != os.getuid() or room.st_mode & 0o007:
        raise OSError("directory %s has unsafe permissions" % directory)


def bind_and_listen_on_posix_socket(socket_name: str, accept_callback: Callable):
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
    old_umask = os.umask(int("0027", 8))

    socket_name, sock = _bind_posix_socket(socket_name)

    _ = os.umask(old_umask)

    sock.listen(0)

    logger.info("Listening on %r." % socket_name)
    return PosixSocketListener(socket_name, sock, accept_callback)


class PosixSocketListener:
    "A bound socket, and the coroutine that takes the clients of it."

    def __init__(self, socket_name: str, sock, accept_callback: Callable) -> None:
        self.socket_name = socket_name
        self.socket = sock
        self._accept_callback = accept_callback

    async def serve(self) -> None:
        "Take every client that connects, until this task is cancelled."
        while True:
            await anyio.wait_readable(self.socket)

            connection, _client_address = self.socket.accept()
            # The socket goes to `PosixSocketConnection` non blocking,
            # and every send waits for room before it retries. A client
            # that stopped reading must not park the loop inside a
            # send: the partial-send loop in `_send_all` is also what
            # keeps a packet whole on OS X, where a non blocking send
            # of more than the buffer holds used to lose the rest.
            # Lillecarl/pymux#418.
            self._accept_callback(PosixSocketConnection(connection))

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


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
        s.bind(socket_name)
        return socket_name, s
    else:
        room = socket_directory()
        i = 0
        while True:
            try:
                socket_name = "%s/pymux.sock.%s.%i" % (
                    room,
                    getpass.getuser(),
                    i,
                )
                s.bind(socket_name)
                return socket_name, s
            except (OSError, socket.error):
                i += 1

                # When 100 times failed, cancel server
                if i == 100:
                    logger.warning(
                        "100 times failed to listen on posix socket. "
                        "Please clean up old sockets."
                    )
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


class PosixSocketConnection(PipeConnection):
    """
    A single active posix pipe connection on the server side.
    """

    def __init__(self, socket) -> None:
        self.socket = socket
        # Non blocking. A send that finds no room parks this
        # connection's write and never the loop (`write`).
        # Lillecarl/pymux#418.
        self.socket.setblocking(False)
        self._recv_buffer = b""
        self._closed = False

        #: Bytes given to `write` and not yet taken by the kernel.
        self._outstanding = 0
        self._write_lock: "anyio.Lock | None" = None

    async def read(self) -> bytes:
        r"""
        Coroutine that reads the next packet.
        (Packets are \0 separated.)
        """
        if self._closed:
            raise BrokenPipeError

        # Read until we have a \0 in our buffer.
        while b"\0" not in self._recv_buffer:
            self._recv_buffer += await self._read_chunk()

        # Split on the first separator.
        pos = self._recv_buffer.index(b"\0")

        packet = self._recv_buffer[:pos]
        self._recv_buffer = self._recv_buffer[pos + 1 :]

        return packet

    async def _read_chunk(self) -> bytes:
        "Wait for this socket to say something, and take what it said."
        if self.socket.fileno() == -1:  # Socket closed.
            raise BrokenPipeError

        try:
            await anyio.wait_readable(self.socket)
        except (anyio.ClosedResourceError, OSError):
            # `close()` says the socket is going, so that this wakes
            # rather than waiting on a descriptor that is taken away.
            raise BrokenPipeError

        try:
            data = self.socket.recv(1024)
        except OSError as e:
            # On OSX, when we try to create a new window by typing "pymux
            # new-window" in a centain pane, very often we get the following
            # error: "OSError: [Errno 9] Bad file descriptor."
            # This doesn't seem very harmful, and we can just try again.
            logger.warning(
                "Got OSError while reading data from client: %s. Trying again.", e
            )
            return b""

        if not data:
            raise BrokenPipeError

        return data

    async def write(self, message: str) -> None:
        """
        Write the next packet. (Packets are \\0 separated.)

        **A client that stopped reading stops only itself.** The send
        waits for room on its own connection and the loop goes on:
        list-sessions, a later attach and every other client of the
        server keep working while this one holds a full buffer. Past
        `TOO_SLOW_BYTES` the client is left behind. Lillecarl/pymux#418.
        """
        if self._closed:
            raise BrokenPipeError

        data = message.encode("utf-8") + b"\0"

        # Counted before the lock, so that a write waiting for its
        # turn is counted while it waits: the lock serialises the
        # packets (one send, then the next, never interleaved), and
        # this count is what bounds how much they may hold together.
        if self._outstanding + len(data) > TOO_SLOW_BYTES:
            raise BrokenPipeError
        self._outstanding += len(data)

        try:
            if self._write_lock is None:
                self._write_lock = anyio.Lock()
            async with self._write_lock:
                await self._send_all(data)
        finally:
            self._outstanding -= len(data)

    async def _send_all(self, data: bytes) -> None:
        "Send every byte, waiting for room rather than blocking the loop."
        view = memoryview(data)
        while view:
            try:
                sent = self.socket.send(view)
            except BlockingIOError:
                try:
                    await anyio.wait_writable(self.socket)
                except (anyio.ClosedResourceError, OSError):
                    # `close()` says the socket is going (it calls
                    # `notify_closing`, which wakes this), or the peer
                    # is gone. Either way this connection is over.
                    raise BrokenPipeError
                continue
            except OSError:
                raise BrokenPipeError
            view = view[sent:]

    def close(self) -> None:
        """
        Close connection.
        """
        if self._closed:
            return
        self._closed = True
        try:
            # Wake whatever is parked on this descriptor **before** the
            # descriptor goes. A reader that learns about the close
            # afterwards waits on a number the kernel has already given
            # to somebody else.
            anyio.notify_closing(self.socket)
        except Exception:
            pass  # No loop here: nothing is parked on it either.
        try:
            self.socket.close()
        except OSError:
            pass
