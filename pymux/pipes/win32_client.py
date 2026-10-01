import ctypes

from .win32 import connect_to_pipe, read_message_from_pipe, write_message_to_pipe

#: The loaded Win32 library. `ctypes` exposes it on Windows only, so it
#: is reached by name rather than imported.
windll = getattr(ctypes, "windll")

__all__ = [
    "PipeClient",
]


class PipeClient:
    r"""
    Windows pipe client.

    :param pipe_name: Name of the pipe. E.g. \\.\pipe\pipe_name
    """

    def __init__(self, pipe_name: str) -> None:
        self.pipe_handle = connect_to_pipe(pipe_name)

    async def write_message(self, text):
        """
        (coroutine)
        Write message into the pipe.
        """
        await write_message_to_pipe(self.pipe_handle, text)

    async def read_message(self):
        """
        (coroutine)
        Read one single message from the pipe and return as text.
        """
        message = await read_message_from_pipe(self.pipe_handle)
        return message

    def close(self):
        """
        Close the connection.
        """
        windll.kernel32.CloseHandle(self.pipe_handle)
