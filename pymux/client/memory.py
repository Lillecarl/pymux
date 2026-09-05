"""
A client that reaches a server in its own process.

`PosixClient` connects to a unix socket, so it reaches whatever server
holds that socket. That server can be an older build, and then a change
that was just made seems to do nothing. This client reads a queue that
the server in this process writes, so there is nothing else it can
reach.

The packets are the same ones. Only the transport differs, and both
sides of the protocol run, so this route proves what the socket route
proves, in one process instead of two.
"""
import asyncio
import json
import signal
import sys

from prompt_toolkit.input.vt100 import raw_mode

from ..pipes import BrokenPipeError, MemoryConnection
from .terminal import TerminalClient

__all__ = [
    "MemoryClient",
]


class MemoryClient(TerminalClient):
    """
    The client half of `pymux integrated`.

    The server runs in the same event loop, so this client cannot block
    on `select`: it reads the keyboard through the loop and waits for
    the packets of the server the same way.
    """

    def __init__(self, connection: MemoryConnection) -> None:
        super().__init__()
        self.connection = connection

    def _send_packet(self, data) -> None:
        "Send to the server. (The queue has no limit, so this waits for nothing.)"
        try:
            self.connection.write_nowait(json.dumps(data))
        except BrokenPipeError:
            pass  # The server is gone. The read loop ends on its own.

    async def attach(
        self, detach_other_clients: bool = False, color_depth=None
    ) -> None:
        """
        Attach the user interface, and return when it ends.

        It ends when the server closes this connection: the last pane
        exited, somebody ran `kill-server`, or this client detached.
        The process ends with it, because the server is in it.
        """
        loop = asyncio.get_running_loop()
        stdin_fd = sys.stdin.fileno()

        self._start_gui(detach_other_clients, color_depth)

        with raw_mode(stdin_fd):
            loop.add_reader(stdin_fd, self._process_stdin)
            try:
                loop.add_signal_handler(signal.SIGWINCH, self._send_size)
            except (NotImplementedError, ValueError):
                pass  # No signals here. The size stays as it was.

            try:
                while True:
                    try:
                        packet = await self.connection.read()
                    except BrokenPipeError:
                        # The server closed the connection. Put the
                        # terminal of the user back as it was.
                        self._reset_terminal()
                        return
                    self._process(packet)
            finally:
                loop.remove_reader(stdin_fd)
                try:
                    loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, ValueError):
                    pass
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self._set_kitty_flags(0)
