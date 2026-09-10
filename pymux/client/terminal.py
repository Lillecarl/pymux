"""
What every client does with the terminal of the user.

A client has two sides. One side is the terminal: read the keys, write
the frames, ask what the terminal supports, put it back as it was. The
other side is the transport that carries the packets to the server.

This holds the first side. The transport is what a subclass adds: a
unix socket in `posix.py`, a pair of queues in `memory.py`. Both sides
speak the same packets, so a difference between the two routes is a
difference of the transport and nothing else.
"""

import json
import os
import signal
import sys
import webbrowser

from prompt_toolkit.input.posix_utils import PosixStdinReader
from prompt_toolkit.input.vt100 import cooked_mode, raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output, _get_size

from pymux.colors import COLOR_QUERIES, TRUECOLOR_PROBE
from pymux.graphics import CELL_SIZE_QUERY
from pymux.graphics import QUERY_SEQUENCE as GRAPHICS_QUERY
from pymux.utils import nonblocking

from .base import Client

__all__ = [
    "TerminalClient",
]

#: What the client asks the outer terminal at attach time.
#:
#: The replies arrive as input and the server reads them: keyboard
#: flags, the kitty graphics protocol, the cell size, the colour depth,
#: the two colours the terminal draws with, and last the device
#: attributes, which also say whether sixel works. Every terminal
#: answers the device attributes query, so a feature that did not
#: answer before it is not supported.
#:
#: The keyboard query asks for every flag first, so that the reply says
#: which ones the terminal took. A terminal that speaks a part of the
#: protocol only says so that way. The pop puts the terminal back as it
#: was.
#: Does the terminal hold a frame back until it is complete?
#: ("CSI ? 2026 $ p", DECRQM.) A terminal that does answers
#: "CSI ? 2026 ; <state> $ y" with a state of 1, 2 or 4; one that does
#: not answers 0, or does not answer at all.
SYNCHRONIZED_OUTPUT_QUERY = b"\x1b[?2026$p"

DETECTION_QUERIES = (
    b"\x1b[>31u\x1b[?u\x1b[<u"
    + SYNCHRONIZED_OUTPUT_QUERY
    + GRAPHICS_QUERY.encode("ascii")
    + CELL_SIZE_QUERY.encode("ascii")
    + TRUECOLOR_PROBE.encode("ascii")
    + COLOR_QUERIES.encode("ascii")
    + b"\x1b[c"
)


class TerminalClient(Client):
    """
    The terminal side of a client. A subclass adds the transport.

    A subclass has to give `_send_packet`, and an `attach` that reads
    the transport and the keyboard until one of them ends.
    """

    def __init__(self) -> None:
        self._mode_context_managers = []

        # Kitty keyboard protocol state of the outer terminal. Whether
        # the terminal supports the protocol is detected at attach time
        # ("CSI ? u" query + device attributes). The currently enabled
        # flags follow the focused pane.
        self._kitty_supported = False
        self._kitty_flags = None

        self.__stdin_reader = None

    @property
    def _stdin_reader(self) -> PosixStdinReader:
        """
        The keyboard, opened when something first reads it.

        **Not in `__init__`.** A client that only runs a command never
        reads the keyboard, and asking for `sys.stdin.fileno()` is not
        free: a pymux command in a script, in a cron job, or under a
        test runner that replaced stdin has no such file, and building
        the client raised before it had sent anything.

        Some terminals, like lxterminal, send non UTF-8 input sequences
        even when the input encoding is supposed to be UTF-8. This
        happens for mouse clicks in the right area of a wide terminal:
        binary blobs in between the UTF-8. They must not be replaced,
        because that breaks the decoding of what follows, and
        `errors="ignore"` does not work either, because a mouse
        sequence is a fixed number of bytes.
        """
        if self.__stdin_reader is None:
            self.__stdin_reader = PosixStdinReader(sys.stdin.fileno(), errors="replace")
        return self.__stdin_reader

    def _send_packet(self, data):
        "Send one packet to the server. (The transport gives this.)"
        raise NotImplementedError

    def _start_gui(self, detach_other_clients: bool, color_depth) -> None:
        """
        Tell the server that this client wants the user interface, and
        ask the outer terminal what it supports.
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

        os.write(sys.stdout.fileno(), DETECTION_QUERIES)
        self._send_packet({"cmd": "kitty-detect"})

    def _reset_terminal(self) -> None:
        """
        Put the terminal of the user back as it was. (The server is
        gone, or the client is leaving.)
        """
        output = Vt100_Output.from_pty(sys.stdout)
        self._set_kitty_flags(0)
        output.quit_alternate_screen()
        output.disable_mouse_support()
        output.disable_bracketed_paste()
        output.reset_attributes()
        output.flush()

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

        elif packet["cmd"] == "open":
            # The server asks this machine, not the machine of the
            # server, to open the URL. `webbrowser` picks the way this
            # platform does it: "open" on macOS, "xdg-open" or what
            # $BROWSER names on Linux, "startfile" on Windows. A browser
            # that fails to open stays silent: this client has no
            # screen of its own to say it on.
            webbrowser.open(packet["data"])

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

        # Send the input in chunks, so that a long paste reaches the
        # server as keystrokes rather than as one packet the server
        # only dispatches when the whole of it has arrived.
        #
        # Nothing enforces the size. `_send_packet` sets the socket
        # blocking and hands `send` whatever it is given, so this is a
        # choice and not a limit. Four thousand and ninety six is a
        # page, and the forty bytes under it are room for the JSON
        # around the data: `{"cmd": "in", "data": ""}` is twenty five
        # of them.
        #
        # The forty do not bound the packet. JSON escapes a control
        # character as six bytes, and a paste is mostly control
        # characters in no case at all, so a full chunk can still pass
        # a page. It bounds how much arrives at once, which is what it
        # is for.
        step = 4096 - 40
        for i in range(0, len(data), step):
            self._send_packet(
                {
                    "cmd": "in",
                    "data": data[i : i + step],
                }
            )

    def the_size(self):
        "The rows and columns of the terminal this client draws on."
        return _get_size(sys.stdout.fileno())

    def _send_size(self):
        "Report terminal size to server."
        rows, cols = self.the_size()
        self._send_packet({"cmd": "size", "data": [rows, cols]})
