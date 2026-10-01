from abc import ABC, abstractmethod

__all__ = [
    "PipeConnection",
    "BrokenPipeError",
]


class PipeConnection(ABC):
    """
    A single active Win32 pipe connection on the server side.

    - Win32PipeConnection
    """

    @abstractmethod
    async def read(self) -> bytes | str:
        """
        Read a single message from the pipe.

        A unix socket and an in-process pipe give bytes; a Win32 pipe
        hands back the text its reader decoded. This can raise
        BrokenPipeError.
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


class BrokenPipeError(Exception):
    "Raised when trying to write to or read from a broken pipe."
