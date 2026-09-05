"""
A pipe connection that carries packets in memory.

The server and a client exchange packets over a unix socket. Nothing in
that exchange needs an operating system: the packets are JSON, and one
end writes what the other end reads. This module gives the same pair of
ends, joined by two queues instead of a socket.

With it, a server and a client run in one process. That is what
`pymux integrated` does. The point is not speed. A client that connects
to a socket reaches whatever server holds that socket, which can be an
older build; a client that reads a queue reaches the server in its own
process, and nothing else.
"""
import asyncio
from typing import Tuple

from .base import BrokenPipeError, PipeConnection

__all__ = [
    "MemoryConnection",
    "connect_in_memory",
]

#: What an end puts on the queue of its peer when it closes. A read
#: that takes this sees the end of the connection.
_CLOSED = object()


class MemoryConnection(PipeConnection):
    """
    One end of an in-memory connection.

    An end reads its own queue and writes the queue of its peer. Both
    queues are unbounded, so a write never waits and the packets arrive
    in the order they were written.
    """

    def __init__(self) -> None:
        self._incoming: "asyncio.Queue" = asyncio.Queue()
        self._peer: "MemoryConnection" | None = None
        self._closed = False

    def _join(self, peer: "MemoryConnection") -> None:
        self._peer = peer
        peer._peer = self

    async def read(self) -> bytes:
        """
        Take the next packet. Raise `BrokenPipeError` at the end.
        """
        if self._closed:
            raise BrokenPipeError

        packet = await self._incoming.get()

        if packet is _CLOSED:
            self._closed = True
            raise BrokenPipeError

        return packet

    def write_nowait(self, message: str) -> None:
        """
        Give the next packet to the peer, without waiting.

        The queue of the peer has no limit, so a write never waits.
        The client side uses this: it writes from the keyboard reader
        and from the signal handler, which are not coroutines.

        The packet is encoded the way the socket route encodes it, so
        that the other end reads the same bytes on both routes.
        """
        if self._closed or self._peer is None or self._peer._closed:
            raise BrokenPipeError

        self._peer._incoming.put_nowait(message.encode("utf-8"))

    async def write(self, message: str) -> None:
        """
        Give the next packet to the peer.
        """
        self.write_nowait(message)

    def close(self) -> None:
        """
        Close this end. Tell the peer, so that a read of it ends.
        """
        if self._closed:
            return
        self._closed = True
        if self._peer is not None:
            self._peer._incoming.put_nowait(_CLOSED)


def connect_in_memory() -> Tuple[MemoryConnection, MemoryConnection]:
    """
    Return the two ends of one connection: (server_end, client_end).

    Give the first to a `ServerConnection` and the second to a client.
    """
    server_end = MemoryConnection()
    client_end = MemoryConnection()
    server_end._join(client_end)
    return server_end, client_end
