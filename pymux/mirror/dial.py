"""
Reach another server's socket, here or over SSH, as one byte stream.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
from libpymux.protocol import READ_SIZE

from ..client.defaults import is_ssh_url
from ..client.ssh import open_socket, ssh_target

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection, SSHReader, SSHWriter

__all__ = ["dial"]


async def dial(address: str, **connect_with: Any) -> anyio.abc.ByteStream:
    """
    A stream to the pymux socket `address` names: a path on this
    machine, or `ssh://[user@]host[:port][/path]`, which reaches the
    socket the way `pymux -S ssh://...` does. `connect_with` goes to
    asyncssh, for a test's key and hosts file.
    """
    if not is_ssh_url(address):
        return await anyio.connect_unix(address)
    connection, reader, writer, _path = await open_socket(ssh_target(address), connect_with)
    return _SshStream(connection, reader, writer)


class _SshStream(anyio.abc.ByteStream):
    "One SSH channel to a unix socket, and the connection it lives on."

    def __init__(self, connection: SSHClientConnection, reader: SSHReader[bytes], writer: SSHWriter[bytes]) -> None:
        self._connection = connection
        self._reader = reader
        self._writer = writer

    async def receive(self, max_bytes: int = READ_SIZE) -> bytes:
        try:
            data = await self._reader.read(max_bytes)
        except OSError as error:
            raise anyio.BrokenResourceError from error
        if not data:
            raise anyio.EndOfStream
        return data

    async def send(self, item: bytes) -> None:
        try:
            self._writer.write(item)
            await self._writer.drain()
        except OSError as error:
            raise anyio.BrokenResourceError from error

    async def send_eof(self) -> None:
        with contextlib.suppress(OSError):
            self._writer.write_eof()

    async def aclose(self) -> None:
        self._connection.close()
        with contextlib.suppress(Exception):
            await self._connection.wait_closed()
