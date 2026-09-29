"""
A pymux client whose terminal is a pyte screen.

A pane stream shows a pane's own screen, and what pymux draws over a pane
is not on it: copy mode, the clock, a popup, a prompt, the status line.
A client draws all of those, so this is a client -- the same `start-gui`
a terminal's client sends -- and pyte stands where the terminal would.
`PaneView` then sends that screen to a browser exactly as it sends a
pane's. Lillecarl/pymux#481.

**pyte is the outer terminal, in both directions.** pymux's client sets
the keyboard modes it wants on its terminal, so a key a browser names is
spelled by this screen's `encode_key_event`, and the answers a terminal
gives to a query go back as input. Nothing here knows an escape sequence.

**It is a client, so it counts as one.** It sits in `list-clients`, and
unless it is read-only it votes on the size of a window. A pane stream is
the thing to use for a viewer that should leave no mark.

It holds no socket: `send` is how a packet leaves, and `take_packet` is
how one arrives, so a unix socket, a test's pipe and a relay are three
callers of one thing.
"""

import json
from typing import Any, Callable, Dict, Optional

from pyte.keys import Unhearable
from pyte.modes import PrivateMode
from pyte.screen import Screen
from pyte.streams import Stream

from pymux.key_spelling import event_however_it_is_written
from pymux.protocol import Packet
from pymux.web.protocol import PaneView, Typed, typed_of

__all__ = ["SessionScreen"]

#: What this terminal says it is. pyte answers as xterm does, and holds
#: 24-bit colour, so the client need not guess down to 256.
TERM = "xterm-256color"
COLORTERM = "truecolor"
COLOR_DEPTH = "DEPTH_24_BIT"


class SessionScreen:
    """
    One client of a pymux server, drawn on a pyte screen.

    `send` takes a packet as the server reads it: a dict with `cmd`.
    """

    def __init__(
        self,
        rows: int,
        columns: int,
        send: Callable[[Dict[str, Any]], None],
        read_only: bool = False,
    ) -> None:
        self._send = send
        self.read_only = read_only
        self.screen = Screen(rows, columns, write_process_input=self._answer)
        self._stream = Stream(self.screen)
        self._view = PaneView()

    # -- the client side of the wire -----------------------------------

    def start(self) -> None:
        "Ask for the user interface, the way a terminal's client does."
        self._send_size()
        self._send(
            {
                "cmd": Packet.START_GUI,
                "detach-others": False,
                "read-only": self.read_only,
                "color-depth": COLOR_DEPTH,
                "term": TERM,
                "colorterm": COLORTERM,
            }
        )

    def take_packet(self, packet: Dict[str, Any]) -> bool:
        """
        Read one packet from the server. True when the connection ends.

        Output is fed and nothing else: a frame is asked for separately,
        because a server writes a frame as several packets and one frame
        a viewer sees per packet would be most of them half drawn.
        """
        kind = packet.get("cmd")
        if kind == Packet.OUT:
            self._stream.feed(packet["data"])
        elif kind in (Packet.EXIT, Packet.ERR):
            return True
        return False

    def resize(self, rows: int, columns: int) -> None:
        "The viewer's element took another size."
        self.screen.resize(rows, columns)
        self._send_size()

    def _send_size(self) -> None:
        self._send(
            {"cmd": Packet.SIZE, "data": [self.screen.lines, self.screen.columns]}
        )

    def _answer(self, data: str) -> None:
        "What the terminal says back to a query goes in as input."
        self._send({"cmd": Packet.IN, "data": data})

    # -- the viewer side -----------------------------------------------

    def welcome(self) -> Dict[str, Any]:
        return self._view.welcome(self.screen, self.screen.writes, not self.read_only)

    def frame(self) -> Optional[Dict[str, Any]]:
        "What the viewer has not seen, or None."
        return self._view.frame(self.screen, self.screen.writes)

    def take(self, data: str) -> Optional[str]:
        """
        Read one message from the viewer, and answer why not if refused.

        The same messages a pane stream takes. They become input of this
        client, which pymux reads as it reads a person's keyboard, so its
        bindings, its prefix and copy mode all answer.
        """
        try:
            message = json.loads(data)
        except ValueError:
            return "that is not JSON"
        if not isinstance(message, dict):
            return "a message is an object"
        try:
            typed = typed_of(message)
        except ValueError as refused:
            return str(refused)

        # A read-only client refuses keys in the server as well; this says
        # why instead of dropping them.
        if self.read_only:
            return "this client only watches"

        try:
            self._send({"cmd": Packet.IN, "data": self._spelled(typed)})
        except Unhearable as refused:
            return str(refused)
        return None

    def _spelled(self, typed: Typed) -> str:
        """
        The bytes a terminal would send for what the viewer typed.

        **This screen spells them, because it is the terminal.** pymux's
        client asks its terminal for a keyboard protocol, and the answer
        is held here and nowhere else.
        """
        if not typed.named:
            if typed.bracketed and PrivateMode.BRACKETED_PASTE.flag in self.screen.mode:
                return "\x1b[200~%s\x1b[201~" % (typed.text,)
            return typed.text

        spelled = []
        for name in typed.text.split():
            try:
                event = event_however_it_is_written(name)
            except ValueError:
                # Not a key anybody names; `send-keys` types it as text.
                spelled.append(name)
                continue
            spelled.append(self.screen.encode_key_event(event, exactly=True))
        return "".join(spelled)
