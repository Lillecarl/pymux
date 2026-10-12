from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = [
    "BrokenPipeError",
    "PipeConnection",
]


class PipeConnection(ABC):
    "One connection between a server and a client: a unix socket, or queues in one process."

    @abstractmethod
    async def read(self) -> dict:
        """
        Read the next packet, decoded: JSON or msgpack, as the peer turned
        to (`libpymux.wire`).

        This can raise BrokenPipeError.
        """

    #: Whether `write` can turn its stream to zstd (`compress_after`).
    can_compress = False

    @abstractmethod
    async def write(self, frame: bytes, compress_after: bool = False) -> None:
        """
        Write one encoded packet, its framing included
        (`libpymux.wire.encode_json` or `encode_msgpack`). With
        `compress_after`, every write after this one goes into one zstd
        stream, in the order the writes go out.

        This can raise BrokenPipeError.
        """

    @abstractmethod
    def close(self):
        """
        Close connection.
        """

    @property
    def outstanding(self) -> int:
        "Bytes given to `write` that the other end has not taken yet."
        return 0

    async def drained(self) -> None:
        "Wait until `outstanding` is zero."


class BrokenPipeError(Exception):
    "Raised when trying to write to or read from a broken pipe."
