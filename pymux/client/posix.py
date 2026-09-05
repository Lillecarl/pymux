import getpass
import glob
import json
import os
import signal
import socket
import sys
import tempfile
from select import select

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
        self.socket.setblocking(1)

    def run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command. Print the output that the server
        sends back, and return the exit code of the command.

        :param pane_id: Optional identifier of the current pane.
        """
        self._send_packet({"cmd": "run-command", "data": command, "pane_id": pane_id})

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

                if packet["cmd"] == "out":
                    sys.stdout.write(packet["data"])
                    sys.stdout.flush()
                elif packet["cmd"] == "err":
                    sys.stderr.write(packet["data"])
                    sys.stderr.flush()
                elif packet["cmd"] == "exit":
                    exit_code = packet["code"]

        return exit_code

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
                            # End of file. Connection closed.
                            # Reset terminal
                            self._reset_terminal()
                            return
                        else:
                            data_buffer += data

                            while b"\0" in data_buffer:
                                pos = data_buffer.index(b"\0")
                                self._process(data_buffer[:pos])
                                data_buffer = data_buffer[pos + 1 :]

                    elif stdin_fd in r:
                        # Got user input.
                        self._process_stdin()

            finally:
                signal.signal(signal.SIGWINCH, signal.SIG_IGN)
                # Restore the keyboard mode of the outer terminal, also
                # when the loop ends through an exception.
                self._set_kitty_flags(0)

    def _send_packet(self, data):
        "Send to server."
        data = json.dumps(data).encode("utf-8")

        # Be sure that our socket is blocking, otherwise, the send() call could
        # raise `BlockingIOError` if the buffer is full.
        self.socket.setblocking(1)

        self.socket.send(data + b"\0")


def list_socket_names():
    """
    The socket of every server that is running, the newest one first.

    A server with no name takes the lowest number that is free, so the
    oldest server usually holds "pymux.sock.<user>.0". `glob` gives no
    order at all, and "pymux attach" takes the first name it reads. So
    a person who started a second server and attached could land on
    either one, and usually landed on the old one.

    The time of the socket file is the time the server started, because
    nothing writes to a socket file after the bind. Newest first means
    that "pymux attach" reaches the server a person just started, which
    is what they mean by it.
    """
    pattern = "%s/pymux.sock.%s.*" % (tempfile.gettempdir(), getpass.getuser())
    return sorted(glob.glob(pattern), key=_started_at, reverse=True)


def _started_at(path: str) -> float:
    "When the server bound this socket. A socket that went away is oldest."
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def list_clients():
    """
    A client for every server that is running, the newest one first.

    A server that no longer answers is left out.
    """
    for path in list_socket_names():
        try:
            yield PosixClient(path)
        except socket.error:
            pass
