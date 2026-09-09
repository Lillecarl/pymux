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

#: How often this client reads the size of its terminal, in seconds.
#: `Application.terminal_size_polling_interval` is the same number, and
#: for the same reason: it is short enough that a person who drags a
#: window does not wait for it, and one `ioctl` twice a second costs
#: nothing.
SIZE_INTERVAL = 0.5


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

            # Held, so that nothing collects it while it waits. A task
            # nobody holds dies in silence and takes its exception with
            # it.
            watcher = loop.create_task(self._watch_the_size())

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
                watcher.cancel()
                loop.remove_reader(stdin_fd)
                try:
                    loop.remove_signal_handler(signal.SIGWINCH)
                except (NotImplementedError, ValueError):
                    pass
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self._set_kitty_flags(0)

    async def _watch_the_size(self) -> None:
        """
        Tell the server whenever the terminal has a new size.

        The signal handler above does this too, and in this process the
        server's application takes that signal away. So this is what
        actually reports a resize, and the handler is what reports it
        at once when nothing has taken the signal yet.

        Only a change is sent. A packet on every turn would ask the
        server to lay every window out twice a second.
        """
        last = self.the_size()

        while True:
            await asyncio.sleep(SIZE_INTERVAL)

            size = self.the_size()
            if size != last:
                last = size
                self._send_size()
