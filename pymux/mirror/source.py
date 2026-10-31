"""
One pane, lent to one peer: the owner's side of a mirror.

**What a peer holds is what this queued, in order.** A read is queued in
the follower call that the pane's own read makes, so the queue holds the
screen's history exactly. A freeze is queued the same way, in one
synchronous step, so no read can fall between the freeze and its place
in the queue.

**A pause stops the queue growing, never empties it.** The reads already
queued still go, and `Freezer.caught_up` marks the screen as it stands
after the last of them: that is the screen the peer will hold. The freeze
on resume then holds exactly what changed since. Dropping queued reads
would leave the peer behind the mark, and the freeze would miss the rows
written in between. `pyte/tests/test_mirroring.py` proves the rule.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpymux.protocol import Field, Message, Packet
from pyte.freeze import Freezer, freeze, freeze_value

from ..log import logger

if TYPE_CHECKING:
    from ptterm.control import TerminalControl
    from pyte.surroundings import Surroundings

    from pymux.core.pane import Pane

    from .link import PeerLink

__all__ = ["LAG_BYTES", "MirrorSource"]

#: How many bytes of one mirror may wait to be written before it stops
#: sending reads. A peer that cannot keep up then gets the rows that
#: changed once it drained, which is smaller than every read in between
#: for a program that redraws, and never larger than its screen.
LAG_BYTES = 1 << 20


class MirrorSource:
    "A follower of one pane's control, which queues what the peer must hear."

    def __init__(self, link: PeerLink, pane: Pane, control: TerminalControl) -> None:
        self.link = link
        self.pane = pane
        self.control = control
        self.freezer = Freezer()
        #: The peer said nobody there watches this pane.
        self.paused = False
        #: More than `LAG_BYTES` waited to be written, so reads stopped.
        self.lagging = False
        #: Bytes queued for this mirror and not yet written.
        self.queued = 0
        self._history_limit: int | None = None

    def start(self) -> None:
        self.control.followers.append(self)
        self._send_screen()

    def stop(self) -> None:
        # Paused, so the reads still queued draining sends no freeze.
        self.paused = True
        if self in self.control.followers:
            self.control.followers.remove(self)

    def pause(self) -> None:
        if not self.paused and not self.lagging:
            self.freezer.caught_up(self.control.screen)
        self.paused = True

    def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        if not self.lagging:
            self._send_screen()

    def sent(self, size: int) -> None:
        "The link wrote `size` bytes of this mirror."
        self.queued -= size
        if self.lagging and self.queued == 0:
            self.lagging = False
            if not self.paused:
                self._send_screen()

    @property
    def sending(self) -> bool:
        return not self.paused and not self.lagging

    # -- the follower --------------------------------------------------

    def read(self, data: str, expired: bool) -> None:
        if not self.sending:
            return
        limit = self.control.screen.get_history_limit()
        if limit != self._history_limit:
            self._history_limit = limit
            self._send({Field.CMD: Packet.MIRROR_HISTORY_LIMIT, Field.HISTORY_LIMIT: limit})
        self._send({Field.CMD: Packet.MIRROR_READ, Field.DATA: data, Field.EXPIRED: expired})
        if self.queued > LAG_BYTES:
            self.lagging = True
            self.freezer.caught_up(self.control.screen)

    def resized(self, lines: int, columns: int) -> None:
        if self.sending:
            self._send({Field.CMD: Packet.MIRROR_RESIZED, Field.LINES: lines, Field.COLUMNS: columns})

    def surrounded(self, surroundings: Surroundings) -> None:
        if self.sending:
            self._send({Field.CMD: Packet.MIRROR_SURROUNDED, Field.SURROUNDINGS: freeze_value(surroundings)})

    # -- sending -------------------------------------------------------

    def _send_screen(self) -> None:
        control = self.control
        try:
            screen = list(self.freezer.freeze(control.screen))
            stream = list(freeze(control.stream))
        except Exception:
            logger.exception("Freezing pane %s for a peer failed.", self.pane.pane_id)
            self.link.lost(self)
            return
        self._history_limit = control.screen.get_history_limit()
        self._send(
            {
                Field.CMD: Packet.MIRROR_SCREEN,
                # Lists, because msgpack refuses a NamedTuple.
                Field.SCREEN: screen,
                Field.STREAM: stream,
                Field.SURROUNDINGS: freeze_value(control.screen.surroundings),
                Field.HISTORY_LIMIT: self._history_limit,
            }
        )

    def _send(self, packet: Message) -> None:
        """
        Queue a packet. **Never raises**: a follower runs inside the
        pane's own read, and a peer's trouble may not stop the pane. A
        packet that cannot be sent ends this mirror instead.
        """
        packet[Field.PANE_ID] = int(self.pane.pane_id)
        try:
            self.queued += self.link.queue(self, packet)
        except Exception:
            logger.exception("Sending pane %s to a peer failed.", self.pane.pane_id)
            self.link.lost(self)
