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
    `$TMUX_TMPDIR`; then `$XDG_RUNTIME_DIR`, the user-private runtime
    directory Linux already holds, which dies with the session and
    takes stale sockets with it; then what Python names the temp
    directory, which on macOS is the user's own `$TMPDIR`.

    A room that fails the check is a refusal, never a repair: a
    directory somebody else built is exactly the one not to use. A
    base that cannot hold a room falls through to the next base.
    """
    bases = []
    for name in ("PYMUX_TMPDIR", "XDG_RUNTIME_DIR"):
        value = os.environ.get(name)
        # A relative base means another thing after `daemonize` moves
        # the process to /. Lillecarl/pymux#322.
        if value and os.path.isabs(value):
            bases.append(value)
    bases.append(tempfile.gettempdir())

    for base in bases:
        directory = os.path.join(base, "pymux-%d" % os.getuid())
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        _verify_the_socket_room(directory)
        return directory

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
            # Note: We don't have to put this socket in non blocking mode.
            #       This can cause crashes when sending big packets on OS X.

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


class PosixSocketConnection(PipeConnection):
    """
    A single active posix pipe connection on the server side.
    """

    def __init__(self, socket) -> None:
        self.socket = socket
        self._recv_buffer = b""
        self._closed = False

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
        Write the next packet. (The socket takes it at once.)
        """
        try:
            self.socket.send(message.encode("utf-8") + b"\0")
        except socket.error:
            if not self._closed:
                raise BrokenPipeError

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
