"""
The wire of `connection.py`, for a caller that runs a loop.

The bytes and their meaning are `command_packet` and `Reply`, which do
no I/O, so this and `Connection` say the same thing to the server.
"""

from __future__ import annotations

from collections.abc import Sequence

import anyio
import anyio.abc

from .connection import (
    CommandResult,
    Reply,
    ServerNotRunning,
    as_command,
    checked,
    command_packet,
)

__all__ = ["AsyncConnection"]


class AsyncConnection:
    """
    One pymux server, addressed by the path of its socket.

    Like `Connection`, it holds no socket of its own: the server closes
    the connection when a command ends, so each command opens one.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def __repr__(self) -> str:
        return "AsyncConnection(%r)" % (self.socket_path,)

    async def is_alive(self) -> bool:
        "True when a server answers on this socket."
        try:
            stream = await self._connect()
        except ServerNotRunning:
            return False
        await stream.aclose()
        return True

    async def run(
        self,
        command: str | Sequence[str],
        pane_id: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        "`Connection.run`, awaited."
        text = as_command(command)
        reply = Reply()
        async with await self._connect() as stream:
            await stream.send(command_packet(text, pane_id))
            while True:
                try:
                    data = await stream.receive()
                except anyio.EndOfStream, anyio.ClosedResourceError, OSError:
                    break
                reply.feed(data)
        return checked(text, reply.result(), check)

    async def _connect(self) -> anyio.abc.SocketStream:
        try:
            return await anyio.connect_unix(self.socket_path)
        except OSError as error:
            raise ServerNotRunning("no pymux server on %s" % (self.socket_path,)) from error
