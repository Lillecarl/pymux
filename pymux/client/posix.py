from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import time
from select import select
from typing import override

from libpymux.protocol import Field, Framer, Packet
from libpymux.sockets import servers_newest_first
from prompt_toolkit.input.vt100 import raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.agentic import caller_cwd, caller_environment

from .reconnect import draw
from .terminal import DELETE_EVERY_IMAGE, TerminalClient

#: How often a client waiting for a restarted server tries the socket,
#: in seconds.
RETRY_EVERY = 0.1

__all__ = [
    "PosixClient",
    "list_clients",
]


class PosixClient(TerminalClient):
    """
    A client that reaches the server over a unix socket.

    The socket is what makes a client and a server two processes. See
    `pymux.client.memory` for the other route, where they are one.
    """

    def __init__(self, socket_name):
        super().__init__()
        self.socket_name = socket_name

        # Connect to socket.
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(socket_name)
        self.socket.setblocking(True)

    @override
    def run_command(self, command, pane_id=None, timeout=None) -> int:
        """
        Ask the server to run this command. Print the output that the server
        sends back, and return the exit code of the command.

        :param pane_id: Optional identifier of the current pane.
        :param timeout: How long the answer may take, in seconds, when a
            number is given. A server always closes the connection after
            the answer, so the read ends with it; the number is for a
            peer that never answers at all -- something else listens on
            the path. `None`, the default, waits the way the attach
            always has.
        """
        self._send_packet(
            {
                Field.CMD: Packet.RUN_COMMAND,
                Field.DATA: command,
                Field.PANE_ID: pane_id,
                # Who and from where. The server cannot read the
                # caller's context itself, so the client sends the
                # whole environment -- resolved to one id under
                # `PYMUX_AGENTIC_ID` beside it -- and the directory it
                # stands in. `pymux/agentic.py` holds the names.
                Field.ENVIRONMENT: {**dict(os.environ), **caller_environment()},
                Field.CWD: caller_cwd(),
            }
        )
        if timeout is not None:
            # After the send, which asked for blocking again: the read
            # is the side that needs the patience.
            self.socket.settimeout(timeout)

        # Read the answer of the server. Packets:
        #   "out": output of the command. (stdout)
        #   "err": errors. (stderr)
        #   "exit": exit code of the command.
        exit_code = 0
        framer = Framer()

        while True:
            try:
                data = self.socket.recv(4096)
            except OSError:
                break

            if not data:
                break  # Connection closed.

            for packet_data in framer.feed(data):
                packet = json.loads(packet_data.decode("utf-8"))

                if packet[Field.CMD] == Packet.OUT:
                    sys.stdout.write(packet[Field.DATA])
                    sys.stdout.flush()
                elif packet[Field.CMD] == Packet.ERR:
                    sys.stderr.write(packet[Field.DATA])
                    sys.stderr.flush()
                elif packet[Field.CMD] == Packet.EXIT:
                    exit_code = packet[Field.CODE]

        return exit_code

    @override
    def attach(self, detach_other_clients: bool = False, color_depth=None):
        """
        Attach client user interface.
        """
        self._start_gui(detach_other_clients, color_depth)

        with raw_mode(sys.stdin.fileno()):
            framer = Framer()

            stdin_fd = sys.stdin.fileno()

            try:

                def winch_handler(signum, frame):
                    self._send_size()

                signal.signal(signal.SIGWINCH, winch_handler)
                while True:
                    socket_fd = self.socket.fileno()
                    r, _, _ = select([stdin_fd, socket_fd], [], [])

                    if socket_fd in r:
                        # Received packet from server.
                        try:
                            data = self.socket.recv(1024)
                        except OSError:
                            # Connection lost. (E.g. the server process
                            # died.) Same as end of file.
                            data = b""

                        if data == b"":
                            # End of file. A server that said it is
                            # restarting is waited for; any other is
                            # gone, and the way out through the
                            # `finally` puts the terminal back -- once,
                            # whatever ended the loop. Lillecarl/pymux#404.
                            if self.restart_wait is not None and self._wait_for_the_next_server(stdin_fd):
                                framer = Framer()
                                self._start_gui(detach_other_clients, color_depth)
                                continue
                            return
                        for packet in framer.feed(data):
                            self._process(packet)

                    elif stdin_fd in r:
                        # Got user input. An ended stdin is the
                        # person's input side going away, and the
                        # client leaves on it, the same way an ended
                        # socket above ends the loop: `select` reports
                        # an ended fd ready forever, and left alone the
                        # loop spun at full CPU on empty reads.
                        # Lillecarl/pymux#419.
                        if not self._process_stdin():
                            return

            finally:
                signal.signal(signal.SIGWINCH, signal.SIG_IGN)
                self._leave_the_terminal()

    def _wait_for_the_next_server(self, stdin_fd) -> bool:
        """
        Connect to the server that follows a restart, on the same socket.

        Waits `restart_wait` seconds at most, and says so on the screen
        with a way out: q leaves. Returns whether a server answered.
        The modes and keyboard flags the old server pushed come off
        first; the new one pushes its own. Lillecarl/pymux#409.
        """
        wait = self.restart_wait
        if wait is None:
            return False
        self.restart_wait = None
        # The old socket stays open until a new one replaces it: a
        # resize sends on it meanwhile, and a send to a closed peer is
        # a broken pipe `_send_packet` ignores, where a closed socket
        # raises.
        self._restore_modes()
        self._pop_kitty_flags()
        # The old server's images, which the next server knows nothing
        # of and would draw its own over. Lillecarl/pymux#399.
        if self.placed_images:
            os.write(sys.stdout.fileno(), DELETE_EVERY_IMAGE)
            self.placed_images = False

        output = Vt100_Output.from_pty(sys.stdout)
        size = output.get_size()
        draw(
            output,
            size.rows,
            size.columns,
            [
                "pymux is restarting.",
                "Waiting up to %d seconds for it on %s." % (wait, self.socket_name),
                "",
                "Press q to leave.",
            ],
        )

        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            attempt = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                attempt.connect(self.socket_name)
            except OSError:
                attempt.close()
            else:
                attempt.setblocking(True)
                self.socket.close()
                self.socket = attempt
                return True

            if select([stdin_fd], [], [], RETRY_EVERY)[0]:
                pressed = os.read(stdin_fd, 1024)
                if not pressed or b"q" in pressed:
                    break

        self.exit_code = 1
        return False

    @override
    def _send_packet(self, data):
        """
        Send to server, and say nothing when the server has gone.

        **A server that refuses an attach writes the reason and closes.**
        `ServerConnection._refuse_the_attach` awaits both writes first,
        so the reason is on its way; the client is still in its
        handshake at that moment, and its next packet meets a socket
        with no reader. Raising there loses the reason: the person reads
        "BrokenPipeError: [Errno 32] Broken pipe" where "that terminal
        is taken" belongs, and on a loaded machine the close wins that
        race. Lillecarl/pymux#479.

        Nothing is lost by saying nothing here. The read loop is what
        learns that a connection ended, and it already treats an ended
        socket as the end of the attachment; what the server sent before
        it closed is in the buffer on this side, waiting to be read.
        """
        data = json.dumps(data).encode("utf-8")

        # Be sure that our socket is blocking, otherwise, the send() call could
        # raise `BlockingIOError` if the buffer is full.
        self.socket.setblocking(True)

        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.socket.send(data + b"\0")


def list_clients():
    """
    A client for every server that is running, the newest one first.

    A server that no longer answers is left out. `servers_newest_first`
    says why that order and not another, and it is where the room is
    read: this globbed the same two patterns for itself, and the two
    answers differed in the order and in whether they asked if a name
    was a socket at all. Lillecarl/pymux#451.
    """
    for path in servers_newest_first():
        with contextlib.suppress(OSError):
            yield PosixClient(path)
