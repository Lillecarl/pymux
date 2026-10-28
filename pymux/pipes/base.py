from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from contextlib import asynccontextmanager

import anyio
from libpymux.protocol import Message

__all__ = [
    "BrokenPipeError",
    "PipeConnection",
    "Turns",
]


class Turns:
    """
    Whose turn it is to write, one writer at a time, in the order they
    came: but an urgent writer goes ahead of every writer that waits.

    `anyio.Lock` is first come, first served, so a packet of sound waited
    behind every part of a window pane's frame that was queued before it.
    Here it waits only for the part already going out. Lillecarl/pymux#657.
    """

    def __init__(self) -> None:
        self._held = False
        self._urgent: deque[anyio.Event] = deque()
        self._waiting: deque[anyio.Event] = deque()

    @asynccontextmanager
    async def turn(self, urgent: bool = False):
        await self._acquire(urgent)
        try:
            yield
        finally:
            self._release()

    async def _acquire(self, urgent: bool) -> None:
        if not self._held:
            self._held = True
            return
        mine = anyio.Event()
        queue = self._urgent if urgent else self._waiting
        queue.append(mine)
        try:
            await mine.wait()
        except BaseException:
            if mine in queue:
                queue.remove(mine)
            elif mine.is_set():
                # The turn came, and its writer is gone: pass it on.
                self._release()
            raise

    def _release(self) -> None:
        queue = self._urgent or self._waiting
        if queue:
            # Handed over, still held.
            queue.popleft().set()
        else:
            self._held = False


class PipeConnection(ABC):
    "One connection between a server and a client: a unix socket, or queues in one process."

    #: The bytes this end sent and the bytes it took in, as the wire
    #: carried them: after zstd, so a client over ssh counts what the
    #: network moved. Lillecarl/pymux#677.
    bytes_out: int
    bytes_in: int

    @abstractmethod
    async def read(self) -> Message:
        """
        Read the next packet, decoded: JSON or msgpack, as the peer turned
        to (`libpymux.wire`).

        This can raise BrokenPipeError.
        """

    #: Whether `write` can turn its stream to zstd (`compress_after`).
    can_compress = False

    @abstractmethod
    async def write(self, frame: bytes, compress_after: bool = False, urgent: bool = False) -> None:
        """
        Write one encoded packet, its framing included
        (`libpymux.wire.encode_json` or `encode_msgpack`). With
        `compress_after`, every write after this one goes into one zstd
        stream, in the order the writes go out. An `urgent` write goes
        ahead of every write that waits for its turn: sound.

        This can raise BrokenPipeError.
        """

    @abstractmethod
    def close(self):
        """
        Close connection.
        """

    async def linger(self) -> None:
        "Close, and let the far end read what it was sent before it goes: the last thing a connection does."
        self.close()

    @property
    def outstanding(self) -> int:
        "Bytes given to `write` that the other end has not taken yet."
        return 0

    #: Whether `outstanding` is the client's terminal falling behind,
    #: so frames wait for it rather than pile up. A socket bounds its
    #: own instead, and leaves a client behind past `TOO_SLOW_BYTES`.
    #: Lillecarl/pymux#585.
    holds_frames_while_behind = False

    async def drained(self) -> None:
        "Wait until `outstanding` is zero."


class BrokenPipeError(Exception):
    "Raised when trying to write to or read from a broken pipe."
