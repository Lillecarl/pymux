import getpass
import glob
import json
import os
import signal
import socket
import sys
import tempfile
from select import select

from prompt_toolkit.input.posix_utils import PosixStdinReader
from prompt_toolkit.input.vt100 import cooked_mode, raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output, _get_size

from pymux.colors import TRUECOLOR_PROBE
from pymux.graphics import CELL_SIZE_QUERY
from pymux.graphics import QUERY_SEQUENCE as GRAPHICS_QUERY
from pymux.utils import nonblocking

from .base import Client

__all__ = [
    "PosixClient",
    "list_clients",
]


class PosixClient(Client):
    def __init__(self, socket_name):
        self.socket_name = socket_name
        self._mode_context_managers = []

        # Kitty keyboard protocol state of the outer terminal. Whether
        # the terminal supports the protocol is detected at attach time
        # ("CSI ? u" query + device attributes). The currently enabled
        # flags follow the focused pane.
        self._kitty_supported = False
        self._kitty_flags = None

        # Connect to socket.
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(socket_name)
        self.socket.setblocking(1)

        # Input reader.
        #     Some terminals, like lxterminal send non UTF-8 input sequences,
        #     even when the input encoding is supposed to be UTF-8. This
        #     happens in the case of mouse clicks in the right area of a wide
        #     terminal. Apparently, these are some binary blobs in between the
        #     UTF-8 input.)
        #     We should not replace these, because this would break the
        #     decoding otherwise. (Also don't pass errors='ignore', because
        #     that doesn't work for parsing mouse input escape sequences, which
        #     consist of a fixed number of bytes.)
        self._stdin_reader = PosixStdinReader(sys.stdin.fileno(), errors="replace")

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
        self._send_size()
        self._send_packet(
            {
                "cmd": "start-gui",
                "detach-others": detach_other_clients,
                "color-depth": color_depth,
                "term": os.environ.get("TERM", ""),
                "colorterm": os.environ.get("COLORTERM", ""),
                "data": "",
            }
        )

        # Ask the outer terminal what it supports. The replies arrive
        # as input and are interpreted by the server: keyboard flags,
        # the kitty graphics protocol, the cell size, the colour depth,
        # and last the device attributes, which also say whether sixel
        # works. Every terminal answers the device attributes query, so
        # a feature that did not answer before it is not supported.
        os.write(
            sys.stdout.fileno(),
            b"\x1b[?u"
            + GRAPHICS_QUERY.encode("ascii")
            + CELL_SIZE_QUERY.encode("ascii")
            + TRUECOLOR_PROBE.encode("ascii")
            + b"\x1b[c",
        )
        self._send_packet({"cmd": "kitty-detect"})

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
                            o = Vt100_Output.from_pty(sys.stdout)
                            self._set_kitty_flags(0)
                            o.quit_alternate_screen()
                            o.disable_mouse_support()
                            o.disable_bracketed_paste()
                            o.reset_attributes()
                            o.flush()
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

    def _process(self, data_buffer):
        """
        Handle incoming packet from server.
        """
        packet = json.loads(data_buffer.decode("utf-8"))

        if packet["cmd"] == "out":
            # Call os.write manually. In Python2.6, sys.stdout.write doesn't use UTF-8.
            os.write(sys.stdout.fileno(), packet["data"].encode("utf-8"))

        elif packet["cmd"] == "suspend":
            # Suspend client process to background.
            if hasattr(signal, "SIGTSTP"):
                os.kill(os.getpid(), signal.SIGTSTP)

        elif packet["cmd"] == "kitty-keyboard":
            # Kitty keyboard protocol instructions for the outer
            # terminal.
            data = packet["data"]
            if "supported" in data:
                self._kitty_supported = data["supported"]
            if "flags" in data:
                self._set_kitty_flags(data["flags"])

        elif packet["cmd"] == "mode":
            # Set terminal to raw/cooked.
            action = packet["data"]

            if action == "raw":
                cm = raw_mode(sys.stdin.fileno())
                cm.__enter__()
                self._mode_context_managers.append(cm)

            elif action == "cooked":
                cm = cooked_mode(sys.stdin.fileno())
                cm.__enter__()
                self._mode_context_managers.append(cm)

            elif action == "restore" and self._mode_context_managers:
                cm = self._mode_context_managers.pop()
                cm.__exit__()

    def _set_kitty_flags(self, flags: int) -> None:
        """
        Enable the given kitty keyboard protocol flags on the outer
        terminal. (Ignored when the terminal does not support the
        protocol. Zero restores the legacy encoding.)
        """
        if not self._kitty_supported and flags != 0:
            return
        flags = flags or 0
        if flags == self._kitty_flags:
            return
        if flags == 0 and self._kitty_flags is None:
            return  # Never enabled anything.
        self._kitty_flags = flags
        os.write(sys.stdout.fileno(), ("\x1b[=%d;1u" % flags).encode())

    def _process_stdin(self):
        """
        Received data on stdin. Read and send to server.
        """
        with nonblocking(sys.stdin.fileno()):
            data = self._stdin_reader.read()

        # Send input in chunks of 4k.
        step = 4056
        for i in range(0, len(data), step):
            self._send_packet(
                {
                    "cmd": "in",
                    "data": data[i : i + step],
                }
            )

    def _send_packet(self, data):
        "Send to server."
        data = json.dumps(data).encode("utf-8")

        # Be sure that our socket is blocking, otherwise, the send() call could
        # raise `BlockingIOError` if the buffer is full.
        self.socket.setblocking(1)

        self.socket.send(data + b"\0")

    def _send_size(self):
        "Report terminal size to server."
        rows, cols = _get_size(sys.stdout.fileno())
        self._send_packet({"cmd": "size", "data": [rows, cols]})


def list_clients():
    """
    List all the servers that are running.
    """
    p = "%s/pymux.sock.%s.*" % (tempfile.gettempdir(), getpass.getuser())
    for path in glob.glob(p):
        try:
            yield PosixClient(path)
        except socket.error:
            pass
