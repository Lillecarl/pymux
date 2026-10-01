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

import asyncio
import json
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import anyio

from pyte.keys import Unhearable
from pyte.modes import PrivateMode
from pyte.screen import Screen
from pyte.streams import GroundTimer, Stream

from pymux.key_spelling import event_however_it_is_written
from pymux.log import logger
from libpymux.protocol import Field, Packet
from pymux.web.protocol import PaneView, Typed, typed_of

__all__ = ["SessionScreen", "run_session"]

#: What this terminal says it is. pyte answers as xterm does, and holds
#: 24-bit colour, so the client need not guess down to 256.
TERM = "xterm-256color"
COLORTERM = "truecolor"
COLOR_DEPTH = "DEPTH_24_BIT"

#: What a viewer says when its element changed size.
SIZE = "size"

#: The most rows or columns a viewer may ask for. A screen of a thousand
#: by a thousand is a million cells, and a number past that is a mistake
#: or somebody seeing what happens.
LARGEST = 1000

#: How much of the socket to read at once, as `libpymux` does.
_CHUNK = 65536

#: How long a sequence may stay open before the next bytes drop it, the
#: same five seconds a pane uses. Lillecarl/pymux#485.
_GROUND_TIMEOUT = 5


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
        self._ground_timer = GroundTimer(self._stream, _GROUND_TIMEOUT, time.monotonic)
        self._view = PaneView()

    # -- the client side of the wire -----------------------------------

    def start(self) -> None:
        "Ask for the user interface, the way a terminal's client does."
        self._send_size()
        self._send(
            {
                Field.CMD: Packet.START_GUI,
                Field.DETACH_OTHERS: False,
                Field.READ_ONLY: self.read_only,
                Field.COLOR_DEPTH: COLOR_DEPTH,
                Field.TERM: TERM,
                Field.COLORTERM: COLORTERM,
            }
        )

    def take_packet(self, packet: Dict[str, Any]) -> bool:
        """
        Read one packet from the server. True when the connection ends.

        Output is fed and nothing else: a frame is asked for separately,
        because a server writes a frame as several packets and one frame
        a viewer sees per packet would be most of them half drawn.
        """
        kind = packet.get(Field.CMD)
        if kind == Packet.OUT:
            self._ground_timer.feed(packet[Field.DATA])
        elif kind in (Packet.EXIT, Packet.ERR):
            return True
        return False

    def resize(self, rows: int, columns: int) -> None:
        "The viewer's element took another size."
        self.screen.resize(rows, columns)
        self._send_size()

    def _send_size(self) -> None:
        self._send(
            {Field.CMD: Packet.SIZE, Field.DATA: [self.screen.lines, self.screen.columns]}
        )

    def _answer(self, data: str) -> None:
        "What the terminal says back to a query goes in as input."
        self._send({Field.CMD: Packet.IN, Field.DATA: data})

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

        # A pane has a size of its own, and a client does not: only the
        # viewer knows how many cells its element holds.
        if message.get("type") == SIZE:
            rows, columns = message.get("rows"), message.get("columns")
            if (
                not isinstance(rows, int)
                or not isinstance(columns, int)
                or not 0 < rows <= LARGEST
                or not 0 < columns <= LARGEST
            ):
                return "a size message needs rows and columns from 1 to %d" % LARGEST
            self.resize(rows, columns)
            return None

        try:
            typed = typed_of(message)
        except ValueError as refused:
            return str(refused)

        # A read-only client refuses keys in the server as well; this says
        # why instead of dropping them.
        if self.read_only:
            return "this client only watches"

        try:
            self._send({Field.CMD: Packet.IN, Field.DATA: self._spelled(typed)})
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


async def run_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    to_viewer: Callable[[Dict[str, Any]], Awaitable[None]],
    from_viewer: AsyncIterator[str],
    rows: int,
    columns: int,
    read_only: bool = False,
) -> None:
    """
    Be one client of the server at the other end of `reader` and `writer`,
    for one viewer, until either of them stops.

    **A frame goes out per read of the socket, not per packet.** The
    server writes one frame as several packets, and a viewer sent one
    frame each would be shown most of them half drawn. A read takes what
    has arrived, which is usually the whole frame.
    """

    def send(packet: Dict[str, Any]) -> None:
        writer.write(json.dumps(packet).encode("utf-8") + b"\0")

    session = SessionScreen(rows, columns, send, read_only=read_only)
    session.start()
    await to_viewer(session.welcome())

    async with anyio.create_task_group() as both:

        async def from_the_server() -> None:
            buffer = b""
            while True:
                data = await reader.read(_CHUNK)
                if not data:
                    break
                buffer += data
                ended = False
                while b"\0" in buffer:
                    raw, buffer = buffer.split(b"\0", 1)
                    if raw and session.take_packet(json.loads(raw.decode("utf-8"))):
                        ended = True
                frame = session.frame()
                if frame is not None:
                    await to_viewer(frame)
                if ended:
                    break
            both.cancel_scope.cancel()

        async def from_the_viewer() -> None:
            async for message in from_viewer:
                refused = session.take(message)
                if refused is not None:
                    logger.info("A viewer's message was refused: %s", refused)
                await writer.drain()
            both.cancel_scope.cancel()

        both.start_soon(from_the_server)
        both.start_soon(from_the_viewer)
