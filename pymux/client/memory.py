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

**One process is one SIGWINCH handler, and that used to be a real
difference.** The server's `prompt_toolkit.Application` took the signal
for itself while it ran: `attach_winch_signal_handler` calls
`loop.add_signal_handler`, which replaces whatever was there, and
asyncio holds one callback per signal. On the socket route the server
is another process and cannot reach this one's handler. Here it could,
and it did, so this client stopped hearing that the terminal had
changed size: a font size change in kitty moved nothing, because the
size the server had was the one this client last reported.
Lillecarl/pymux#208.

**A server's application now says it does not want the signal**, which
is the honest statement of the same fact: it has no terminal of its own
to be resized, and every client reports its size in a packet.
`run_async(handle_sigwinch=False)` in `ServerConnection._create_app`,
and the flag is Lillecarl/pymux#231.

The size is polled as well, and that stays. prompt_toolkit polls for a
reason of its own -- `Application._poll_output_size` names "situations
where `attach_winch_signal_handler` is not sufficient" -- and a size
that is read on a timer costs nothing and covers what a signal misses.
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import override

import anyio
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

    @override
    def _send_packet(self, data) -> None:
        "Send to the server. (The queue has no limit, so this waits for nothing.)"
        # The server is gone. The read loop ends on its own.
        with contextlib.suppress(BrokenPipeError):
            self.connection.write_nowait(json.dumps(data))

    @override
    async def attach(self, detach_other_clients: bool = False, color_depth=None) -> None:
        """
        Attach the user interface, and return when it ends.

        It ends when the server closes this connection: the last pane
        exited, somebody ran `kill-server`, or this client detached.
        The process ends with it, because the server is in it.
        """
        stdin_fd = sys.stdin.fileno()

        self._start_gui(detach_other_clients, color_depth)

        with raw_mode(stdin_fd):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self._read_keyboard, stdin_fd)
                tasks.start_soon(self._watch_signal)
                tasks.start_soon(self._watch_size)

                try:
                    while True:
                        try:
                            packet = await self.connection.read()
                        except BrokenPipeError:
                            return  # The server closed the connection.
                        self._process(packet)
                finally:
                    # The three readers above end with this scope.
                    tasks.cancel_scope.cancel()
                    self._leave_the_terminal()

    async def _read_keyboard(self, stdin_fd: int) -> None:
        """
        Give the server what the person types, until their input side
        is gone.

        An ended stdin is the end of this attachment. Here the server
        lives in this process and this terminal is the one it serves,
        so closing the connection ends the read loop the same way its
        own hangup does, and the attach returns through the
        `BrokenPipeError` that restores the terminal.
        Lillecarl/pymux#419.
        """
        while True:
            await anyio.wait_readable(stdin_fd)
            if not self._process_stdin():
                self.connection.close()
                return
