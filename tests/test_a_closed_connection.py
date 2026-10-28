"""
A server connection that closes lets the far end read what it was sent
first, even when that end writes into the close. Lillecarl/pymux#710,
Lillecarl/pymux#766.
"""

from __future__ import annotations

import asyncio
import socket

import anyio

from pymux.pipes.posix import LINGER_SECONDS, PosixSocketConnection


class Forwarder(asyncio.Protocol):
    "Reads the way asyncssh's forwarder does: through an asyncio transport."

    def __init__(self) -> None:
        self.data = b""
        self.lost: asyncio.Future = asyncio.get_running_loop().create_future()

    def data_received(self, data: bytes) -> None:
        self.data += data

    def connection_lost(self, exc: Exception | None) -> None:
        self.lost.set_result(exc)


async def test_the_last_packet_reaches_a_peer_that_writes_into_the_close():
    """
    The peer has sent bytes the server never read, and writes once more
    after the close. A plain close fails that write, and asyncio then
    drops what it had not read: the hangup's exit packet, or a restart
    notice.
    """
    server, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    connection = PosixSocketConnection(server)
    transport, forwarder = await asyncio.get_running_loop().create_unix_connection(Forwarder, sock=far)
    transport.write(b"pong")

    with anyio.fail_after(LINGER_SECONDS / 2):
        await connection.write(b"the last packet")
        connection.close()
        transport.write(b"an answer")
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(connection.linger)
            assert await forwarder.lost is None

    assert forwarder.data == b"the last packet"
    assert server.fileno() == -1, "linger closes the descriptor once the peer has gone"
