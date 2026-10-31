"""
The peer's side of one mirror: what the owner sends, applied to a
mirrored pane's control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpymux.protocol import Field, Message, Packet
from pyte.freeze import Frozen, thaw_value

from ..connection.packet_table import PacketTable

if TYPE_CHECKING:
    from ptterm.control import TerminalControl

__all__ = ["MirrorSink"]

_PACKETS: PacketTable[MirrorSink] = PacketTable()

#: The history limit of a mirror before the owner says its own: none
#: trims before the first freeze arrives.
_NOT_YET_TOLD = 2000


class MirrorSink:
    """
    Applies the mirror packets of one pane to a control that mirrors it.

    The owner trims its history at each read by its own limit, and the
    mirror has to trim at the same reads by the same one, so the
    screen's limit is the owner's and not this server's option.
    """

    def __init__(self, control: TerminalControl) -> None:
        self.control = control
        control.mirrored = True
        control.screen.get_history_limit = self.history_limit
        self.limit = _NOT_YET_TOLD
        #: Why the owner will not send this pane, once it said so.
        self.refused: str | None = None

    def history_limit(self) -> int:
        return self.limit

    def take(self, packet: Message) -> None:
        _PACKETS.handle(self, packet)

    @_PACKETS.handles(Packet.MIRROR_SCREEN)
    def _on_screen(self, packet: Message) -> None:
        self.limit = packet[Field.HISTORY_LIMIT]
        self.control.catch_up(
            Frozen(*packet[Field.SCREEN]),
            Frozen(*packet[Field.STREAM]),
            thaw_value(packet[Field.SURROUNDINGS]),
        )

    @_PACKETS.handles(Packet.MIRROR_READ)
    def _on_read(self, packet: Message) -> None:
        self.control.feed_read(packet[Field.DATA], bool(packet[Field.EXPIRED]))

    @_PACKETS.handles(Packet.MIRROR_RESIZED)
    def _on_resized(self, packet: Message) -> None:
        self.control.resize_screen(packet[Field.LINES], packet[Field.COLUMNS])

    @_PACKETS.handles(Packet.MIRROR_SURROUNDED)
    def _on_surrounded(self, packet: Message) -> None:
        self.control.screen.surround(thaw_value(packet[Field.SURROUNDINGS]))

    @_PACKETS.handles(Packet.MIRROR_REFUSED)
    def _on_refused(self, packet: Message) -> None:
        self.refused = str(packet.get(Field.ERROR) or "refused")
        # The mirror ends here, so a line of this side's own no longer
        # parts it from the screen it copied.
        self.control.feed_output("\r\n[the owner stopped sending this pane: %s]\r\n" % self.refused)

    @_PACKETS.handles(Packet.MIRROR_HISTORY_LIMIT)
    def _on_history_limit(self, packet: Message) -> None:
        self.limit = packet[Field.HISTORY_LIMIT]
