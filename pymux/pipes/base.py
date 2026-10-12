from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = [
    "BrokenPipeError",
    "PipeConnection",
]


class PipeConnection(ABC):
    "One connection between a server and a client: a unix socket, or queues in one process."

    @abstractmethod
    async def read(self) -> bytes:
        """
        Read a single message from the pipe.

        This can raise BrokenPipeError.
        """

    @abstractmethod
    async def write(self, message: str) -> None:
        """
        Write a single message into the pipe.

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
