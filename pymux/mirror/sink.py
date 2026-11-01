"""
The peer's side of one mirror: the frames the owner sends, laid on a
mirrored pane's control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpymux.protocol import Field, Message, Packet

from ..connection.packet_table import PacketTable
from .copying import Place, place_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from ptterm.control import TerminalControl
    from pyte.images import GraphicsImage

__all__ = ["MirrorSink"]

_PACKETS: PacketTable[MirrorSink] = PacketTable()


class MirrorSink:
    "Applies the mirror packets of one pane to a control that mirrors it."

    def __init__(self, control: TerminalControl, pointer_changed: Callable[[], None]) -> None:
        self.control = control
        #: Told when a frame changes the shape the pointer takes over the pane.
        self.pointer_changed = pointer_changed
        #: Why the owner will not send this pane, once it said so.
        self.refused: str | None = None
        #: Where copy mode on the owner stands, while the frames show it.
        self.copying: Place | None = None
        #: The images the owner had this side store, by hash (`pyte.grid`).
        self.store: dict[str, GraphicsImage] = {}

    def take(self, packet: Message) -> None:
        _PACKETS.handle(self, packet)

    @_PACKETS.handles(Packet.MIRROR_FRAME)
    def _on_frame(self, packet: Message) -> None:
        frame = packet.get(Field.FRAME)
        if not isinstance(frame, dict):
            return
        if Field.COPYING in packet:
            self.copying = place_of(packet[Field.COPYING])
        shapes = self.control.screen.pointer_shapes
        before = shapes.shape
        self.control.put_frame(frame, self.store)
        if shapes.shape != before:
            self.pointer_changed()

    @_PACKETS.handles(Packet.MIRROR_REFUSED)
    def _on_refused(self, packet: Message) -> None:
        self.refused = str(packet.get(Field.ERROR) or "refused")
        # The mirror ends here, so a line of this side's own no longer
        # parts it from the page it copied.
        self.control.feed_output("\r\n[the owner stopped sending this pane: %s]\r\n" % self.refused)
