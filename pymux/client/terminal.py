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
import socket
import sys
import webbrowser

from prompt_toolkit.input.posix_utils import PosixStdinReader
from prompt_toolkit.input.vt100 import cooked_mode, raw_mode
from prompt_toolkit.output.vt100 import Vt100_Output, _get_size

from pymux.colors import COLOR_QUERIES, TRUECOLOR_PROBE
from pymux.config import client_options_in, find_config
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


def _ttyname() -> str:
    """
    The terminal this client draws on, or "" when it has none.

    A client with no terminal is not an error. A pymux command in a
    script or a cron job has its output on a pipe, and the tests run
    one with stdout replaced; the server names those by the process
    instead. Lillecarl/pymux#335.
    """
    try:
        return os.ttyname(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return ""


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

        #: What this client leaves with when it is attached. The server
        #: names it in an `exit` packet, which it sends before it closes
        #: a connection it will not serve: a refused attach is not the
        #: same as a person who detached, and a script has only the
        #: code to tell them apart. Lillecarl/pymux#332.
        self.exit_code = 0

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
                # `-x`: the other clients of the session leave, and the
                # terminals they were in close. Lillecarl/pymux#347.
                "hang-up-others": self.hang_up_others,
                "color-depth": color_depth,
                "term": os.environ.get("TERM", ""),
                "colorterm": os.environ.get("COLORTERM", ""),
                # The machine this client runs on. Only the client can
                # say it: over ssh the server answers `gethostname`
                # with another machine's name. Lillecarl/pymux#287.
                "hostname": socket.gethostname(),
                # The whole environment of this client, of which the
                # server keeps the names "update-environment" lists and
                # drops the rest. The client cannot do the filtering: it
                # is the server that holds the option, and asking for it
                # first would be a round trip at every attach. tmux
                # sends the whole environment too, one message per
                # variable. Lillecarl/pymux#271.
                "environment": dict(os.environ),
                # The terminal this client draws on, and the process it
                # runs as. Both belong to this machine, which is why
                # the server keeps the hostname in front of them: two
                # people can each be on their own `/dev/pts/3`. The
                # process is the fallback for a client with no terminal
                # at all, the way tmux falls back for a control client.
                # Lillecarl/pymux#335.
                "ttyname": _ttyname(),
                "pid": os.getpid(),
                # What this client's own configuration file says about
                # this client. A theme belongs to the terminal a
                # person is sitting at, and only this side can read
                # the file that names it: over SSH the server's
                # configuration is another machine's.
                # Lillecarl/pymux#223.
                "client-options": self._client_options(),
                "data": "",
            }
        )

        os.write(sys.stdout.fileno(), DETECTION_QUERIES)
        self._send_packet({"cmd": "kitty-detect"})

    def _restore_modes(self) -> None:
        """
        Put back every raw or cooked mode the server asked for.

        The server asks for cooked mode while a pane reads a password,
        and asks for it back afterwards. An attachment that ends in
        between leaves the terminal as the pane wanted it, which means
        the keys a person presses next are echoed and held for a line.
        Lillecarl/pymux#256.
        """
        while self._mode_context_managers:
            try:
                self._mode_context_managers.pop().__exit__()
            except Exception:
                pass

    def _client_options(self) -> list:
        """
        What this client announces about itself, in the order the
        server applies it.

        The configuration file first and the command line after, so a
        flag wins: a file belongs to one machine and a flag to the one
        terminal it was typed in. Lillecarl/pymux#223,
        Lillecarl/pymux#340.
        """
        announced = client_options_in(self.config_file or find_config())
        if self.chosen_name:
            announced.append(("name", self.chosen_name))
        return announced

    def _reset_terminal(self) -> None:
        """
        Put the terminal of the user back as it was. (The server is
        gone, or the client is leaving.)
        """
        output = Vt100_Output.from_pty(sys.stdout)
        self._pop_kitty_flags()
        # DECTCEM is not part of what the alternate screen puts back,
        # so a cursor that pymux hid stays hidden in the shell the
        # person returns to.
        output.show_cursor()
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

        elif packet["cmd"] == "exit":
            # The server is about to close this connection, and says
            # what this client leaves with. The read loop ends on the
            # close itself.
            self.exit_code = packet["code"]
            # And whether to hang up the process that started this
            # client on the way out, which is `attach -x`. The signal
            # goes after the terminal is back, so it is remembered
            # here and sent there. Lillecarl/pymux#347.
            if packet.get("hang-up"):
                self.hang_up_asked = True

        elif packet["cmd"] == "suspend":
            # Suspend client process to background.
            if hasattr(signal, "SIGTSTP"):
                os.kill(os.getpid(), signal.SIGTSTP)

        elif packet["cmd"] == "open":
            # The server asks this machine, not the machine of the
            # server, to open the URL. A machine with no browser says
            # so back, where the request was made visible.
            url = packet["data"]
            if not self._open_url(url):
                self._send_packet({"cmd": "open-failed", "data": url})

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
        Put the outer terminal in the keyboard encoding the focused
        pane wants. (Ignored when the terminal does not support the
        protocol.)

        The first enable is a push, the protocol's own way to hold the
        state that was there: a shell's flags, most of all a nested
        pymux's. A later pane asks for different flags and gets a set
        -- our push still holds the restore point, and the value over
        it is ours to change. The way out pops what we pushed
        (`_pop_kitty_flags`), and that is how the nested case gives
        the outer session its encoding back. Lillecarl/pymux#403.
        """
        if not self._kitty_supported and flags != 0:
            return
        if flags == self._kitty_flags:
            return
        if self._kitty_flags is None:
            if flags == 0:
                return  # Nothing of ours is on the stack; 0 changes nothing.
            os.write(sys.stdout.fileno(), ("\x1b[>%du" % flags).encode())
        else:
            os.write(sys.stdout.fileno(), ("\x1b[=%d;1u" % flags).encode())
        self._kitty_flags = flags

    def _pop_kitty_flags(self) -> None:
        """
        Take our push off the outer terminal's stack. The state
        beneath it comes back: the shell's, or a nested pymux's.
        """
        if self._kitty_flags is None:
            return  # Never enabled anything.
        os.write(sys.stdout.fileno(), b"\x1b[<1u")
        self._kitty_flags = None

    def _open_url(self, url: str) -> bool:
        """
        Ask this platform to open the URL in a browser, the way
        `webbrowser` picks: "open" on macOS, "xdg-open" or what $BROWSER
        names on Linux, "startfile" on Windows.

        True is a best effort: an opener can still fail after it
        started. A Linux machine without a display says False without
        trying, because the openers it does have say yes and fail
        anyway: `xdg-open` starts, finds no display and exits, and
        `webbrowser.open` counts the start as success. Knowing sooner
        is worth more than the chance of a `BROWSER` that names a
        browser which runs without a display.
        """
        if (
            os.name == "posix"
            and sys.platform != "darwin"
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        ):
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False

    def _process_stdin(self) -> bool:
        """
        Received data on stdin. Read and send to server.

        Returns False when stdin is over: the person's input side is
        gone, and there is nothing left this client can be given.

        An empty read is not the thing to judge by -- the reader
        returns "" also when nothing was ready to read, and when a
        paste of junk decoded to nothing at all. Its docstring says
        only the `closed` attribute is the end of the file, and that
        is what is reported here.
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

        return not self._stdin_reader.closed

    def size(self):
        "The rows and columns of the terminal this client draws on."
        return _get_size(sys.stdout.fileno())

    def _send_size(self):
        "Report terminal size to server."
        rows, cols = self.size()
        self._send_packet({"cmd": "size", "data": [rows, cols]})
