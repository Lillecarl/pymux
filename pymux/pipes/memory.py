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

from typing import Tuple

import anyio

from .base import BrokenPipeError, PipeConnection

__all__ = [
    "MemoryConnection",
    "connect_in_memory",
]


class MemoryConnection(PipeConnection):
    """
    One end of an in-memory connection.

    An end reads its own stream and writes the stream of its peer. Both
    streams are unbounded, so a write never waits and the packets arrive
    in the order they were written.

    **A stream closes and a queue does not**, which is the whole reason
    this is a memory object stream: the end that goes away closes the
    stream of its peer, and the peer's next read says so rather than
    waiting for a packet that a sentinel value has to stand in for.
    Lillecarl/pymux#87.
    """

    def __init__(self) -> None:
        self._send, self._incoming = anyio.create_memory_object_stream(
            max_buffer_size=float("inf")
        )
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

        try:
            return await self._incoming.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            self._closed = True
            raise BrokenPipeError

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

        try:
            self._peer._send.send_nowait(message.encode("utf-8"))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            raise BrokenPipeError

    async def write(self, message: str) -> None:
        """
        Give the next packet to the peer.
        """
        self.write_nowait(message)

    def close(self) -> None:
        """
        Close this end. Tell the peer, so that a read of it ends.

        The packets already written stay readable: closing the sending
        end of a stream leaves what is in it, and the reader sees the
        end of the stream after the last one.
        """
        if self._closed:
            return
        self._closed = True
        self._send.close()
        if self._peer is not None:
            self._peer._send.close()


def connect_in_memory() -> Tuple[MemoryConnection, MemoryConnection]:
    """
    Return the two ends of one connection: (server_end, client_end).

    Give the first to a `ServerConnection` and the second to a client.
    """
    server_end = MemoryConnection()
    client_end = MemoryConnection()
    server_end._join(client_end)
    return server_end, client_end
