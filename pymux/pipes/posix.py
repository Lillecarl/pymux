import asyncio
import getpass
import os
import socket
import tempfile
from typing import Callable

from ..log import logger
from .base import BrokenPipeError, PipeConnection

__all__ = [
    "bind_and_listen_on_posix_socket",
    "PosixSocketConnection",
]


def bind_and_listen_on_posix_socket(
    socket_name: str, accept_callback: Callable, loop: asyncio.AbstractEventLoop | None = None
):
    """
    :param accept_callback: Called with `PosixSocketConnection` when a new
        connection is established.
    :param loop: The asyncio event loop to listen on.
    """
    if loop is None:
        loop = asyncio.get_running_loop()

    # Set umask for the socket file.
    old_umask = os.umask(int("0027", 8))

    # Bind socket.
    socket_name, socket = _bind_posix_socket(socket_name)

    _ = os.umask(old_umask)

    # Listen on socket.
    socket.listen(0)

    def _accept_cb():
        connection, client_address = socket.accept()
        # Note: We don't have to put this socket in non blocking mode.
        #       This can cause crashes when sending big packets on OS X.

        posix_connection = PosixSocketConnection(connection, loop=loop)

        accept_callback(posix_connection)

    loop.add_reader(socket.fileno(), _accept_cb)

    logger.info("Listening on %r." % socket_name)
    return socket_name


def _bind_posix_socket(socket_name: str | None = None):
    """
    Find a socket to listen on and return it.

    Returns (socket_name, sock_obj)
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    if socket_name:
        s.bind(socket_name)
        return socket_name, s
    else:
        i = 0
        while True:
            try:
                socket_name = "%s/pymux.sock.%s.%i" % (
                    tempfile.gettempdir(),
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

    def __init__(self, socket, loop: asyncio.AbstractEventLoop | None = None):
        self.socket = socket
        self._fd = socket.fileno()
        self._recv_buffer = b""
        self._closed = False
        self._loop = loop

    def _loop_ref(self) -> asyncio.AbstractEventLoop | None:
        "Return the event loop for this connection, if it's still known."
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    async def read(self):
        r"""
        Coroutine that reads the next packet.
        (Packets are \0 separated.)
        """
        if self._closed:
            raise BrokenPipeError

        # Read until we have a \0 in our buffer.
        while b"\0" not in self._recv_buffer:
            self._recv_buffer += await _read_chunk_from_socket(
                self.socket, self._loop_ref()
            )

        # Split on the first separator.
        pos = self._recv_buffer.index(b"\0")

        packet = self._recv_buffer[:pos]
        self._recv_buffer = self._recv_buffer[pos + 1 :]

        return packet

    def write(self, message):
        """
        Coroutine that writes the next packet.
        """
        try:
            self.socket.send(message.encode("utf-8") + b"\0")
        except socket.error:
            if not self._closed:
                raise BrokenPipeError

        loop = self._loop_ref()
        if loop is None:
            return None  # No event loop. (Connection is shutting down.)

        f = loop.create_future()
        f.set_result(None)
        return f

    def close(self):
        """
        Close connection.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.socket.close()
        finally:
            # Make sure to remove the reader from the event loop.
            loop = self._loop_ref()
            if loop is not None:
                try:
                    loop.remove_reader(self._fd)
                except (ValueError, KeyError, RuntimeError):
                    pass


def _read_chunk_from_socket(socket, loop):
    """
    (coroutine)
    Turn socket reading into coroutine.
    """
    fd = socket.fileno()
    f = loop.create_future()

    if fd == -1:  # Socket closed.
        f.set_exception(BrokenPipeError())
        return f

    def read_callback():
        loop.remove_reader(fd)

        # Read next chunk.
        try:
            data = socket.recv(1024)
        except OSError as e:
            # On OSX, when we try to create a new window by typing "pymux
            # new-window" in a centain pane, very often we get the following
            # error: "OSError: [Errno 9] Bad file descriptor."
            # This doesn't seem very harmful, and we can just try again.
            logger.warning(
                "Got OSError while reading data from client: %s. " "Trying again.", e
            )
            f.set_result(b"")
            return

        if data:
            f.set_result(data)
        else:
            f.set_exception(BrokenPipeError())

    loop.add_reader(fd, read_callback)

    return f
