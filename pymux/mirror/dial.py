"""
Reach another server's socket, here or over SSH, as one byte stream.
"""

from __future__ import annotations

import contextlib
import socket
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
from libpymux.protocol import READ_SIZE

from ..client.defaults import is_ssh_url
from ..client.ssh import CLOSE_SECONDS, open_socket, ssh_target

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection, SSHReader, SSHWriter

__all__ = ["abort", "dial", "dial_beside", "ssh_connection_of"]


async def dial(address: str, connect_timeout: float | None = None, **connect_with: Any) -> anyio.abc.ByteStream:
    """
    A stream to the pymux socket `address` names: a path on this
    machine, or `ssh://[user@]host[:port][/path]`, which reaches the
    socket the way `pymux -S ssh://...` does. `connect_timeout` bounds
    an SSH connect, as `relink.CONNECT_TIMEOUT` says. `connect_with`
    goes to asyncssh, for a test's key and hosts file.
    """
    if not is_ssh_url(address):
        return await anyio.connect_unix(address)
    connection, reader, writer, path = await open_socket(ssh_target(address), connect_with, connect_timeout)
    return _SshStream(connection, reader, writer, path, owns_connection=True)


async def dial_beside(address: str, stream: anyio.abc.ByteStream) -> anyio.abc.ByteStream:
    """
    One more stream to the socket `stream` reached. Over SSH it is a
    new channel on the same connection, so a command a peer forwards
    costs no new handshake.
    """
    if isinstance(stream, _SshStream):
        return await stream.beside()
    return await anyio.connect_unix(address)


def ssh_connection_of(stream: anyio.abc.ByteStream) -> SSHClientConnection | None:
    "The SSH connection `stream` lives on, for a forward through it; None on a unix socket."
    return stream.connection if isinstance(stream, _SshStream) else None


def abort(stream: anyio.abc.ByteStream) -> None:
    "End a stream `dial` opened at once, so its reader sees it end: for a link that died while this machine slept."
    if isinstance(stream, _SshStream):
        stream.abort()
        return
    with contextlib.suppress(OSError):
        stream.extra(anyio.abc.SocketAttribute.raw_socket).shutdown(socket.SHUT_RDWR)


def _gone() -> tuple[type[Exception], ...]:
    "What a channel raises when its connection went away: a missed keepalive is `ConnectionLost`, no `OSError`."
    import asyncssh

    return (OSError, asyncssh.ConnectionLost)


class _SshStream(anyio.abc.ByteStream):
    "One SSH channel to a unix socket, and the connection it lives on."

    def __init__(
        self,
        connection: SSHClientConnection,
        reader: SSHReader[bytes],
        writer: SSHWriter[bytes],
        path: str,
        owns_connection: bool,
    ) -> None:
        self.connection = connection
        self._reader = reader
        self._writer = writer
        self._path = path
        self._owns_connection = owns_connection

    async def beside(self) -> _SshStream:
        "Another channel to the same socket, which leaves the connection open when it closes."
        try:
            reader, writer = await self.connection.open_unix_connection(self._path)
        except OSError as error:
            raise anyio.BrokenResourceError from error
        return _SshStream(self.connection, reader, writer, self._path, owns_connection=False)

    def abort(self) -> None:
        "Drop the connection without the goodbye a dead link would never carry."
        self.connection.abort()

    async def receive(self, max_bytes: int = READ_SIZE) -> bytes:
        try:
            data = await self._reader.read(max_bytes)
        except _gone() as error:
            raise anyio.BrokenResourceError from error
        if not data:
            raise anyio.EndOfStream
        return data

    async def send(self, item: bytes) -> None:
        try:
            self._writer.write(item)
            await self._writer.drain()
        except _gone() as error:
            raise anyio.BrokenResourceError from error

    async def send_eof(self) -> None:
        with contextlib.suppress(OSError):
            self._writer.write_eof()

    async def aclose(self) -> None:
        if not self._owns_connection:
            self._writer.close()
            return
        self.connection.close()
        # Bounded as the client bounds its own close: a link that died
        # while this machine slept must not hold the redial.
        with contextlib.suppress(Exception), anyio.move_on_after(CLOSE_SECONDS):
            await self.connection.wait_closed()
