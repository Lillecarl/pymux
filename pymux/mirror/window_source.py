"""
One window pane, lent to one peer: the owner's side of a mirrored
Wayland window. Lillecarl/pymux#888.

The peer is one more viewer of the window. It takes each frame as a
client that encodes pixels itself does (`WindowFrames`): raw rectangles,
or VP9 when the window's `window-codec` says so and the peer decodes it.
**One frame is in flight at a time**, until the peer says it took it,
and only then is the window let draw its next. A peer that watches the
pane nowhere gets no frames, and the window waits as a hidden one does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
from libpymux.protocol import Codec, Field, Message, Packet
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.containers import WritePosition

from ..graphics.window_frames import frame_parts, video_parts
from ..log import logger
from ..wayland.window_pane import now

if TYPE_CHECKING:
    from pymux.core.pane import Pane
    from pymux.wayland.window_pane import WindowWidget

    from .link import PeerLink

__all__ = ["MirrorWindowSource"]


class MirrorWindowSource:
    "A viewer of one window pane, which sends the peer its frames."

    def __init__(self, link: PeerLink, pane: Pane, widget: WindowWidget, codecs: frozenset[str]) -> None:
        self.link = link
        self.pane = pane
        self.widget = widget
        #: The codecs the peer decodes.
        self.codecs = codecs
        self.paused = False
        self.stopped = False
        #: A frame went out and the peer has not said it took it.
        self._in_flight = False
        #: The window drew while one was in flight.
        self._again = False
        #: Set when the peer takes the frame in flight.
        self._taken = anyio.Event()
        #: The serial of the last raw frame sent; None sends a whole one.
        self._serial: int | None = None
        #: The VP9 stream to this peer; a new one starts on a keyframe.
        self._encoder: Any = None

    def start(self) -> None:
        self.send()

    def stop(self) -> None:
        self.stopped = True
        self._taken.set()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self.send()

    def drew(self) -> None:
        "The window drew a frame (`Panes.window_pane_drew`)."
        self.send()

    def taken(self) -> None:
        "The peer took the frame in flight: the next may go."
        self._in_flight = False
        self._taken.set()
        self._taken = anyio.Event()
        if self._again:
            self._again = False
            self.send()

    def send(self) -> None:
        "Send the window's frame as it stands, or once the one in flight is taken."
        if self.stopped or self.paused:
            return
        if self._in_flight:
            self._again = True
            return
        self._in_flight = True
        self.link.connection.spawn(self._send())

    async def _drained(self) -> None:
        "What the window's pacer waits on: the peer took the frame."
        if self._in_flight:
            await self._taken.wait()

    async def _send(self) -> None:
        widget = self.widget
        found = self.link.connection.pymux.place_of_pane_id(self.pane.pane_id)
        codec = str(found[1].options.window_codec) if found is not None else ""
        place = WritePosition(xpos=0, ypos=0, width=widget.columns, height=widget.rows)
        view = widget.host.view_of(self.pane.pane_id, widget, place, self._drained)
        parts: list[Message] = []
        try:
            if codec == Codec.VP9 and codec in self.codecs and view.video is not None and view.encoder is not None:
                if self._encoder is None:
                    self._encoder = view.encoder()
                frame = await view.video(self._encoder)
                self._serial = None
                if frame is not None and (frame[4] or frame[1]):
                    parts = video_parts(0, Codec.VP9, frame, "")
            else:
                self._encoder = None
                raw = await view.rects(self._serial)
                if raw is not None:
                    self._serial = raw[0]
                    if raw[4]:
                        parts = frame_parts(0, raw, "")
        except Exception:
            logger.exception("Reading window pane %s for a peer failed.", self.pane.pane_id)
        if self.stopped or not parts:
            self._in_flight = False
            view.shown(False)
            return
        for part in parts:
            del part[Field.IMAGE]
            part.pop(Field.AFTER, None)
            self.link.queue({**part, Field.CMD: Packet.MIRROR_WINDOW_FRAME, Field.PANE_ID: int(self.pane.pane_id)})
        view.shown(True)

    def input(self, data: dict[str, Any]) -> None:
        "What a person at the peer did in the pane, as `mirror-window-input` carries it."
        widget, host = self.widget, self.widget.host
        kind = data.get("kind")
        if kind == "key":
            modifiers = tuple(int(each) for each in data.get("modifiers") or ())
            host.press(widget.toplevel, modifiers, int(data.get("code", 0)), now())
        elif kind == "text":
            host.type_text(widget.toplevel, str(data.get("text", "")))
        elif kind == "leave":
            host.pointer_leave(widget.toplevel, now())
        elif kind in ("pointer", "scroll"):
            x, y = float(data.get("x", 0)), float(data.get("y", 0))
            cell = Point(x=int(x), y=int(y))
            fraction = (x - int(x), y - int(y))
            wayland = self.link.connection.pymux.wayland
            if host is not wayland:
                return
            if kind == "scroll":
                wayland.scroll(widget.toplevel, cell, int(data.get("steps", 0)), now(), fraction)
            else:
                button = data.get("button")
                wayland.pointer(
                    widget.toplevel,
                    cell,
                    None if button is None else int(button),
                    bool(data.get("pressed")),
                    now(),
                    fraction,
                )
