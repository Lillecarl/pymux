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

**One process is one SIGWINCH handler.** A `prompt_toolkit.Application`
takes the signal for itself while it runs: `attach_winch_signal_handler`
calls `loop.add_signal_handler`, which replaces whatever was there, and
asyncio holds one callback per signal. On the socket route the server
is another process and cannot reach this one's handler. Here it can, and
a server application that took it would leave this client deaf to its
terminal's size: a font size change in kitty would move nothing.
Lillecarl/pymux#208.

**So a server's application says it does not want the signal**, which
is the honest statement of the same fact: it has no terminal of its own
to be resized, and every client reports its size in a packet.
`run_async(handle_sigwinch=False)` in `pymux.connection.attach.create_app`,
and the flag is Lillecarl/pymux#231.

The size is polled as well, and that stays. prompt_toolkit polls for a
reason of its own -- `Application._poll_output_size` names "situations
where `attach_winch_signal_handler` is not sufficient" -- and a size
that is read on a timer costs nothing and covers what a signal misses.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import TYPE_CHECKING, override

import anyio
from libpymux.protocol import Field, Message, Packet
from prompt_toolkit.input.vt100 import raw_mode

from ..pipes.base import BrokenPipeError
from ..pipes.memory import MemoryConnection
from .terminal import TerminalClient

if TYPE_CHECKING:
    from prompt_toolkit.output import ColorDepth

__all__ = [
    "MemoryClient",
]

#: How long the way out waits for the terminal to take the last frame.
DRAIN_SECONDS = 1.0


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
        #: Frames the terminal has not taken yet. The server in this
        #: process skips frames while there are any, and draws what
        #: changed once they are gone. Lillecarl/pymux#585.
        self._backlog = bytearray()
        self._more: anyio.Event | None = None
        connection.held_for_terminal = lambda: len(self._backlog)

    @override
    def write_terminal(self, data: bytes) -> None:
        """
        Hand the frame to the writer and return. A terminal that stops
        reading -- XOFF, a stalled link, a stopped emulator -- blocked a
        write here, and with it the server that runs on the same loop:
        no command over the socket answered, `kill-server` included.
        """
        self._backlog += data
        if self._more is not None:
            self._more.set()

    async def _write_backlog(self, stdout_fd: int) -> None:
        "Write the backlog whenever the terminal takes more."
        # A regular file is always writable, and epoll refuses one
        # (EPERM): output sent to a file is written as it comes.
        pollable = True
        while True:
            if not self._backlog:
                self._more = anyio.Event()
                await self._more.wait()
                continue
            if pollable:
                try:
                    await anyio.wait_writable(stdout_fd)
                except PermissionError:
                    pollable = False
            try:
                written = os.write(stdout_fd, self._backlog)
            except BlockingIOError:
                continue
            del self._backlog[:written]

    @override
    def send_packet(self, data: Message) -> None:
        "Send to the server. (The queue has no limit, so this waits for nothing.)"
        # The server is gone. The read loop ends on its own.
        with contextlib.suppress(BrokenPipeError):
            self.connection.write_nowait(self._encode(data))

    @override
    async def attach(self, detach_other_clients: bool = False, color_depth: ColorDepth | None = None) -> None:
        """
        Attach the user interface, and return when it ends.

        It ends when the server closes this connection: the last pane
        exited, somebody ran `kill-server`, or this client detached.
        The process ends with it, because the server is in it.
        """
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        self._start_gui(detach_other_clients, color_depth)

        # Non-blocking, so a write takes what the terminal has room for:
        # a blocking write to a pty waits until every byte went.
        was_blocking = os.get_blocking(stdout_fd)
        os.set_blocking(stdout_fd, False)
        with raw_mode(stdin_fd):
            async with anyio.create_task_group() as tasks:
                self.tasks = tasks
                tasks.start_soon(self._write_backlog, stdout_fd)
                tasks.start_soon(self._read_keyboard, stdin_fd)
                tasks.start_soon(self._watch_signal)
                tasks.start_soon(self._watch_size)

                try:
                    while True:
                        try:
                            packet = await self.connection.read()
                        except BrokenPipeError:
                            return  # The server closed the connection.
                        if packet[Field.CMD] == Packet.BROWSER:
                            # Not `Browsers.start`, which would hold the
                            # loop the server runs on while it starts.
                            tasks.start_soon(self.browsers.run, packet, None)
                            continue
                        self._process(packet)
                finally:
                    # The last frame, if the terminal takes it soon; a
                    # frame cut short is worse than a short wait.
                    with anyio.move_on_after(DRAIN_SECONDS, shield=True):
                        while self._backlog:
                            await anyio.sleep(0.01)
                    # The four tasks above end with this scope.
                    tasks.cancel_scope.cancel()
                    self.tasks = None
                    self.browsers.stop_all()
                    os.set_blocking(stdout_fd, was_blocking)
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
