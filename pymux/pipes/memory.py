"""
A pipe connection that carries packets in memory.

The server and a client exchange packets over a unix socket. Nothing in
that exchange needs an operating system: the packets are encoded frames
(`libpymux.wire`), and one end writes what the other end reads. This module gives the same pair of
ends, joined by two queues instead of a socket.

With it, a server and a client run in one process. That is what
`pymux integrated` does. The point is not speed. A client that connects
to a socket reaches whatever server holds that socket, which can be an
older build; a client that reads a queue reaches the server in its own
process, and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import override

import anyio
from libpymux.wire import PacketReader

from .base import BrokenPipeError, PipeConnection

__all__ = [
    "MemoryConnection",
    "connect_in_memory",
]

#: How often `drained` looks again, in seconds.
DRAIN_POLL = 0.02


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
        self._send, self._incoming = anyio.create_memory_object_stream(max_buffer_size=float("inf"))
        self._peer: MemoryConnection | None = None
        self._closed = False
        self._reader = PacketReader()
        self._ready: list[dict] = []
        #: The bytes the client on this end holds for its terminal, which
        #: has not taken them yet. `MemoryClient` sets it.
        #: Lillecarl/pymux#585.
        self.held_for_terminal: Callable[[], int] = lambda: 0

    def _join(self, peer: MemoryConnection) -> None:
        self._peer = peer
        peer._peer = self

    @override
    async def read(self) -> dict:
        """
        Take the next packet. Raise `BrokenPipeError` at the end.
        """
        while not self._ready:
            if self._closed:
                raise BrokenPipeError
            try:
                frame = await self._incoming.receive()
            except anyio.EndOfStream, anyio.ClosedResourceError:
                self._closed = True
                raise BrokenPipeError
            self._ready.extend(self._reader.feed(frame))
        return self._ready.pop(0)

    def write_nowait(self, frame: bytes) -> None:
        """
        Give the next encoded packet to the peer, without waiting.

        The queue of the peer has no limit, so a write never waits.
        The client side uses this: it writes from the keyboard reader
        and from the signal handler, which are not coroutines.

        The frame is the one the socket route sends, so that the other
        end reads the same bytes on both routes.
        """
        if self._closed or self._peer is None or self._peer._closed:
            raise BrokenPipeError

        try:
            self._peer._send.send_nowait(frame)
        except anyio.BrokenResourceError, anyio.ClosedResourceError:
            raise BrokenPipeError

    holds_frames_while_behind = True

    @property
    @override
    def outstanding(self) -> int:
        """
        What the peer's terminal has not taken yet. The queue between
        the two ends takes everything at once; the terminal is where
        the bytes wait.
        """
        return self._peer.held_for_terminal() if self._peer is not None else 0

    @override
    async def drained(self) -> None:
        while self.outstanding and not self._closed:
            await anyio.sleep(DRAIN_POLL)

    @override
    async def write(self, frame: bytes, compress_after: bool = False, urgent: bool = False) -> None:
        """
        Give the next encoded packet to the peer. Nothing here crosses a
        network, so nothing is compressed (`can_compress`).
        """
        self.write_nowait(frame)

    @override
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


def connect_in_memory() -> tuple[MemoryConnection, MemoryConnection]:
    """
    Return the two ends of one connection: (server_end, client_end).

    Give the first to a `ServerConnection` and the second to a client.
    """
    server_end = MemoryConnection()
    client_end = MemoryConnection()
    server_end._join(client_end)
    return server_end, client_end
