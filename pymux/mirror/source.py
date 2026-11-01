"""
One pane, lent to one peer: the owner's side of a mirror.

The peer is sent the pane's page as frames (`pyte.grid`): the rows
that changed since the frame before, and the cursor. **One frame is in
flight at a time.** The link builds it when its writer reaches it, so
it holds the page as it is then, and a change while it waits needs no
second frame. A peer that cannot keep up gets fewer frames of newer
pages, never a queue of old ones. Lillecarl/pymux#759.

While the person at the peer is in copy mode, the frames show copy mode
instead, which runs here (`MirrorCopy`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from libpymux.protocol import Field, Message, Packet
from pyte.grid import GridWriter

from ..log import logger
from .copying import MirrorCopy, Place

if TYPE_CHECKING:
    from ptterm.control import TerminalControl

    from pymux.core.pane import Pane

    from .link import PeerLink

__all__ = ["MirrorSource"]


class MirrorSource:
    "A follower of one pane's control, which sends the peer its page."

    def __init__(self, link: PeerLink, pane: Pane, control: TerminalControl) -> None:
        self.link = link
        self.pane = pane
        self.control = control
        self.writer = GridWriter()
        #: The peer said nobody there watches this pane.
        self.paused = False
        #: A frame waits in the link's queue.
        self.queued = False
        #: A terminal window with the focus shows the mirror (`Focus.sync_panes`).
        self.focused = False
        #: The copy mode the person at the peer is in, which the frames show.
        self.copying: MirrorCopy | None = None

    def start(self) -> None:
        self.control.followers.append(self)
        self.queue()

    def stop(self) -> None:
        self.paused = True
        if self in self.control.followers:
            self.control.followers.remove(self)
        if self.copying is not None:
            self.copying.end()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self.queue()

    def copy(self, scroll: bool = False, at: Place | None = None) -> None:
        "Open copy mode for the peer, one wheel step up with `scroll` or at `at`, unless it is open."
        if self.copying is not None:
            return
        self.copying = MirrorCopy(self, self.control.terminal, at)
        self.writer = GridWriter()
        self.link.connection.spawn(self.copying.run(scroll))

    def copy_ended(self, copy: MirrorCopy) -> None:
        if self.copying is copy:
            self.copying = None
            self.writer = GridWriter()
            self.queue()

    def changed(self) -> None:
        "The pane's picture may have changed (`Follower`)."
        if self.copying is not None:
            self.copying.page_changed()
        else:
            self.queue()

    def queue(self) -> None:
        "Queue a frame unless one waits."
        if self.paused or self.queued:
            return
        self.queued = True
        try:
            self.link.queue_frame(self)
        except Exception:
            # A follower runs inside the pane's own read: a peer's
            # trouble may not stop the pane.
            logger.exception("Sending pane %s to a peer failed.", self.pane.pane_id)
            self.link.lost(self)

    def frame(self) -> Message | None:
        "The packet of the next frame, as the page stands now, or `None` when nothing changed."
        self.queued = False
        if self.paused:
            return None
        screen = self.control.screen
        place = None
        if self.copying is not None:
            # The pane's title stays over its copy mode.
            self.copying.screen.titles.window = screen.titles.window
            screen = self.copying.screen
            place = self.copying.place
        frame = self.writer.frame(screen)
        if frame is None:
            return None
        return {
            Field.CMD: Packet.MIRROR_FRAME,
            Field.PANE_ID: int(self.pane.pane_id),
            Field.FRAME: frame,
            Field.COPYING: None if place is None else list(place),
        }
