from __future__ import annotations

import json
import signal
import socket
import sys
from select import select
from typing import override

from libpymux.protocol import Field, Packet
from libpymux.sockets import servers_newest_first
from prompt_toolkit.input.vt100 import raw_mode

from .terminal import TerminalClient

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
        self._send_packet({Field.CMD: Packet.RUN_COMMAND, Field.DATA: command, Field.PANE_ID: pane_id})
        if timeout is not None:
            # After the send, which asked for blocking again: the read
            # is the side that needs the patience.
            self.socket.settimeout(timeout)

        # Read the answer of the server. Packets:
        #   "out": output of the command. (stdout)
        #   "err": errors. (stderr)
        #   "exit": exit code of the command.
        exit_code = 0
        data_buffer = b""

        while True:
            try:
                data = self.socket.recv(4096)
            except OSError:
                break

            if not data:
                break  # Connection closed.

            data_buffer += data
            while b"\0" in data_buffer:
                pos = data_buffer.index(b"\0")
                packet_data, data_buffer = data_buffer[:pos], data_buffer[pos + 1 :]

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
            data_buffer = b""

            stdin_fd = sys.stdin.fileno()
            socket_fd = self.socket.fileno()

            try:

                def winch_handler(signum, frame):
                    self._send_size()

                signal.signal(signal.SIGWINCH, winch_handler)
                while True:
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
                            # End of file. The connection is gone, and
                            # the way out through the `finally` puts
                            # the terminal back -- once, whatever ended
                            # the loop. Lillecarl/pymux#404.
                            return
                        data_buffer += data

                        while b"\0" in data_buffer:
                            pos = data_buffer.index(b"\0")
                            self._process(data_buffer[:pos])
                            data_buffer = data_buffer[pos + 1 :]

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
                # Take our kitty push off the outer terminal, also when
                # the loop ends through an exception.
                self._pop_kitty_flags()
                # Put back raw mode if the server pushed cooked over
                # us; an attachment that ends in between must not leave
                # the person's keys echoing. Lillecarl/pymux#411.
                self._restore_modes()
                # And put back what the server's bytes set: the
                # alternate screen, the mouse, the cursor, the
                # attributes. A crash used to leave all of those
                # behind. stdout may be the thing that failed, so the
                # original error outranks anything this raises.
                # Lillecarl/pymux#404.
                try:
                    self._reset_terminal()
                except Exception:
                    pass

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

        try:
            self.socket.send(data + b"\0")
        except BrokenPipeError, ConnectionResetError:
            pass


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
        try:
            yield PosixClient(path)
        except OSError:
            pass
