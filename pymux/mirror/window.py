"""
The peer's side of a mirrored window pane: the owner's frames, decoded
into a picture here, and shown to this server's clients as a window pane
of its own would be. Lillecarl/pymux#888.

The picture is the whole window, so a client that starts to show the
pane, or one that fell behind, gets it whole or as the box that changed
since what it holds, as `pysmithay` answers a local window. A person's
keys and pointer go back to the owner, which holds the window.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

import anyio
from libpymux.protocol import Field, Message, Packet
from prompt_toolkit.application.current import get_app

from ..graphics.kitty_frames import Base64Carrier, decoders, frame_commands, rgba
from ..graphics.window_frames import FrameRects, WindowView
from ..log import logger

if TYPE_CHECKING:
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.containers import WritePosition

    from ..core.pane import Pane
    from ..wayland.window_pane import WindowWidget
    from .copying import Place
    from .peer import Peer

__all__ = ["RemoteWindow"]

#: How many frames of damage are kept: a client further behind gets the picture whole.
DAMAGE_KEPT = 64


class RemoteWindow:
    """
    The host of one mirrored window pane (`WindowHost`), and the sink of
    its frames. Its `toplevel` is the owner's pane id.
    """

    #: Copy mode never runs on a window.
    copying: Place | None = None

    def __init__(self, peer: Peer, pane_id: int) -> None:
        self.peer = peer
        self.pane_id = pane_id
        #: The pane here, once it is built.
        self.pane: Pane | None = None
        self.width = 0
        self.height = 0
        #: The window's pixels, as RGB rows.
        self.picture = bytearray()
        #: Counts the frames taken; a client says which it holds.
        self.serial = 0
        #: The first serial of this picture's size: one before it needs it whole.
        self._sized_at = 0
        #: The box each frame changed, by serial.
        self._damage: deque[tuple[int, tuple[int, int, int, int]]] = deque(maxlen=DAMAGE_KEPT)
        #: The parts of the frame on its way.
        self._parts: list[Message] = []
        self._decoder: Any = None
        self.visible = False
        self.stale = False
        self._asked = False
        self._streaming = False
        self._stopped = False
        #: Why the owner will not lend the window, once it said so.
        self.refused: str | None = None

    # -- streaming, as `RemoteProgram` does ------------------------------

    def seen(self, visible: bool) -> bool:
        "Whether a client here shows the pane now (`Peers.sync_sight`). A window never holds a client's frames."
        self.visible = visible
        wanted = visible and not self._stopped
        if wanted == self._streaming:
            return False
        self._streaming = wanted
        if wanted and not self._asked:
            self._asked = True
            self.peer.mirror(self.pane_id, self)
        elif wanted:
            self.peer.resume(self.pane_id)
        else:
            self.peer.pause(self.pane_id)
        return False

    def stop(self) -> None:
        "Stop mirroring. The window stays open on its own server."
        if not self._stopped:
            self._stopped = True
            if self._asked:
                self.peer.stop(self.pane_id)

    # -- frames from the owner -------------------------------------------

    def take(self, packet: Message) -> None:
        "A packet the owner sent for this pane (`Peer._take`)."
        match packet.get(Field.CMD):
            case Packet.MIRROR_WINDOW_FRAME:
                self._parts.append(packet)
                if packet.get(Field.LAST):
                    parts: list[Message] = self._parts
                    self._parts = []
                    pymux = self.peer.tree.pymux
                    if pymux.tasks is not None:
                        pymux.tasks.start_soon(self._take_frame, parts)
            case Packet.MIRROR_REFUSED:
                self.refused = str(packet.get(Field.ERROR) or "refused")
                logger.warning("The owner does not lend window pane %s: %s", self.pane_id, self.refused)

    async def _take_frame(self, parts: list[Message]) -> None:
        last = parts[-1]
        try:
            width, height = int(last.get(Field.WIDTH, 0)), int(last.get(Field.HEIGHT, 0))
            whole = bool(last.get(Field.WHOLE))
            if last.get(Field.CODEC):
                rects = await self._decoded(parts)
            else:
                rects = [
                    (int(x), int(y), int(w), int(h), bytes(pixels))
                    for part in parts
                    for x, y, w, h, pixels in part.get(Field.RECTS) or ()
                ]
            self._apply(width, height, whole, rects)
        except Exception:
            logger.exception("A frame of window pane %s could not be shown.", self.pane_id)
        self.peer.send({Field.CMD: Packet.MIRROR_WINDOW_SHOWN, Field.PANE_ID: self.pane_id})
        if self.pane is not None:
            self.peer.tree.pymux.panes.window_pane_drew(self.pane)

    async def _decoded(self, parts: list[Message]) -> list[tuple[int, int, int, int, bytes]]:
        "The rectangles a VP9 frame changed, decoded off the event loop."
        last = parts[-1]
        if self._decoder is None:
            make = decoders().get(str(last[Field.CODEC]))
            if make is None:
                raise ValueError("this server decodes no %s" % last[Field.CODEC])
            self._decoder = make()
        data = b"".join(bytes(part.get(Field.VIDEO) or b"") for part in parts)
        changed = [tuple(int(each) for each in rect) for rect in last.get(Field.RECTS) or ()]
        decoder = self._decoder
        _width, _height, rects = await anyio.to_thread.run_sync(
            lambda: decoder.decode(data, None if last.get(Field.WHOLE) else changed)
        )
        return [(x, y, w, h, bytes(rgb)) for x, y, w, h, rgb in rects]

    def _apply(self, width: int, height: int, whole: bool, rects: list[tuple[int, int, int, int, bytes]]) -> None:
        "Lay the rectangles on the picture."
        self.serial += 1
        if (width, height) != (self.width, self.height) or len(self.picture) != width * height * 3:
            self.width, self.height = width, height
            self.picture = bytearray(width * height * 3)
            self._sized_at = self.serial
            self._damage.clear()
            whole = True
        row = width * 3
        for x, y, w, h, pixels in rects:
            for line in range(h):
                at = (y + line) * row + x * 3
                self.picture[at : at + w * 3] = pixels[line * w * 3 : (line + 1) * w * 3]
        box = (0, 0, width, height) if whole else _bounds(rects)
        if box is not None:
            self._damage.append((self.serial, box))

    def _since(self, since: int | None) -> FrameRects | None:
        "The picture as a client holding frame `since` needs it: whole, or the box that changed."
        if not self.picture:
            return None
        width, height = self.width, self.height
        oldest = self._damage[0][0] if self._damage else self.serial + 1
        if since is None or since < self._sized_at or since + 1 < oldest:
            return self.serial, True, width, height, [(0, 0, width, height, bytes(self.picture))]
        box = _bounds(box for serial, box in self._damage if serial > since)
        if box is None:
            return self.serial, False, width, height, []
        left, top, w, h = box
        row = width * 3
        pixels = b"".join(
            self.picture[line * row + left * 3 : line * row + (left + w) * 3] for line in range(top, top + h)
        )
        return self.serial, False, width, height, [(left, top, w, h, pixels)]

    # -- `WindowHost` ----------------------------------------------------

    def view_of(
        self, pane_id: int, widget: WindowWidget, write_position: WritePosition, drained: Callable[[], Awaitable[None]]
    ) -> WindowView:
        "How one client here brings the pane up to date: from the picture, never from the owner."

        async def update(outer_id: int, since: int | None) -> tuple[int, str | None, bool]:
            frame = self._since(since)
            if frame is None:
                return 0, None, False
            serial, whole, width, height, rects = frame
            return serial, frame_commands(outer_id, whole, width, height, rects, Base64Carrier()), whole

        async def rects(since: int | None) -> FrameRects | None:
            return self._since(since)

        def shown(_sent: bool) -> None:
            "The owner paces the window by the frames this side took."

        return WindowView(
            pane_id=pane_id,
            x=write_position.xpos,
            y=write_position.ypos,
            width=write_position.width,
            height=write_position.height,
            update=update,
            shown=shown,
            rects=rects,
        )

    def frame(self, toplevel: int) -> tuple[int, int, bytes] | None:
        if not self.picture:
            return None
        return self.width, self.height, bytes(rgba(bytes(self.picture)))

    async def drawn_again(self, toplevels: Iterable[int], timeout: float) -> None:
        "The owner lets its window draw: the picture here is the last frame it sent."

    def configure(self, toplevel: int, columns: int, rows: int) -> None:
        "The owner sizes the pane, from the room the windows have here (`peer-sizes`)."

    def close(self, toplevel: int) -> None:
        self.stop()

    def _input(self, data: dict[str, Any]) -> None:
        if not self._stopped:
            self.peer.send({Field.CMD: Packet.MIRROR_WINDOW_INPUT, Field.PANE_ID: self.pane_id, Field.DATA: data})

    def press(self, toplevel: int, modifiers: tuple[int, ...], code: int, time: int) -> None:
        self._input({"kind": "key", "modifiers": list(modifiers), "code": code})

    def type_text(self, toplevel: int, text: str) -> bool:
        self._input({"kind": "text", "text": text})
        return True

    def _at(self, position: Point) -> dict[str, float]:
        "Where the pointer is, in cells, with where in its cell the client being handled said."
        client = self.peer.tree.pymux.client_of(get_app())
        across, down = (0.5, 0.5) if client is None else client.connection.pointer_fraction
        return {"x": position.x + across, "y": position.y + down}

    def pointer(self, toplevel: int, position: Point, button: int | None, pressed: bool, time: int) -> None:
        self._input({"kind": "pointer", **self._at(position), "button": button, "pressed": pressed})

    def scroll(self, toplevel: int, position: Point, steps: int, time: int) -> None:
        self._input({"kind": "scroll", **self._at(position), "steps": steps})

    def pointer_leave(self, toplevel: int, time: int) -> None:
        self._input({"kind": "leave"})


def _bounds(rects: Iterable[tuple[Any, ...]]) -> tuple[int, int, int, int] | None:
    "The box around rectangles that start `(x, y, w, h)`, or None for none."
    boxes = [rect[:4] for rect in rects if rect[2] and rect[3]]
    if not boxes:
        return None
    left = min(x for x, _y, _w, _h in boxes)
    top = min(y for _x, y, _w, _h in boxes)
    right = max(x + w for x, _y, w, _h in boxes)
    bottom = max(y + h for _x, y, _w, h in boxes)
    return left, top, right - left, bottom - top
