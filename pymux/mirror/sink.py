"""
The peer's side of one mirror: the frames the owner sends, laid on a
mirrored pane's control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpymux.protocol import Field, Message, Packet

from ..connection.packet_table import PacketTable

if TYPE_CHECKING:
    from ptterm.control import TerminalControl

__all__ = ["MirrorSink"]

_PACKETS: PacketTable[MirrorSink] = PacketTable()


class MirrorSink:
    "Applies the mirror packets of one pane to a control that mirrors it."

    def __init__(self, control: TerminalControl) -> None:
        self.control = control
        #: Why the owner will not send this pane, once it said so.
        self.refused: str | None = None

    def take(self, packet: Message) -> None:
        _PACKETS.handle(self, packet)

    @_PACKETS.handles(Packet.MIRROR_FRAME)
    def _on_frame(self, packet: Message) -> None:
        frame = packet.get(Field.FRAME)
        if isinstance(frame, dict):
            self.control.put_frame(frame)

    @_PACKETS.handles(Packet.MIRROR_REFUSED)
    def _on_refused(self, packet: Message) -> None:
        self.refused = str(packet.get(Field.ERROR) or "refused")
        # The mirror ends here, so a line of this side's own no longer
        # parts it from the page it copied.
        self.control.feed_output("\r\n[the owner stopped sending this pane: %s]\r\n" % self.refused)
