"""
The owner's side of a peer link: one connection from another server,
which mirrors any number of this server's panes.

**One link per peer, not one connection per pane.** Everything a peer
hears of this server travels on it: the mirrors, and what is not a
pane's screen at all. One writer drains one queue. A mirror's frame
waits in it as a mark and is built when the writer reaches it
(`MirrorSource.frame`), so a peer that cannot keep up holds up the link
and is then sent the newest page.
"""

from __future__ import annotations

import socket
from collections import deque
from typing import TYPE_CHECKING, ClassVar

import anyio
from libpymux.protocol import OLDEST_PEER_VERSION, PEER_PINGS, PEER_VERSION, Field, Message, Packet
from libpymux.wire import MSGPACK_ZSTD, choose, encode_json, encode_msgpack
from prompt_toolkit.data_structures import Size
from ptterm import Terminal
from pyte.keep import Keep
from pyte.keys.events import DEFAULT_KEY_PROFILE, KeyProfile
from pyte.mouse import MouseAction
from pyte.osc import Osc, color_base_of

from ..connection.packet_table import PacketTable
from ..enums import Woke
from ..ids import PaneId, WindowId
from ..layout.geometry import size_the_panes_of
from ..log import logger
from ..pipes.base import BrokenPipeError
from ..style.style import DEFAULT_THEME
from ..terminals import PeerTerminal
from .copying import place_of
from .source import MirrorSource
from .tree import Tree, tree_of

if TYPE_CHECKING:
    from pymux.connection.server import ServerConnection
    from pymux.core.pane import Pane

__all__ = ["PeerLink"]

#: The method of `PeerLink` that handles each kind of packet.
_PACKETS: PacketTable[PeerLink] = PacketTable()

#: How long a change to the sessions waits for more before the tree
#: goes, so a burst of changes sends one tree.
TREE_SETTLE = 0.05

#: How often the tree is built again with no change announced, for what
#: moves without asking for a frame, such as a pane's running command.
#: It goes only when it differs from the last one sent.
TREE_EVERY = 2.0


def _key_profile(named: object) -> KeyProfile:
    "The profile a peer sent by field names; a field it did not name keeps the default's answer."
    fields = named if isinstance(named, dict) else {}
    return KeyProfile(
        **{name: bool(fields.get(name, getattr(DEFAULT_KEY_PROFILE, name))) for name in KeyProfile._fields}
    )


class PeerLink:
    "The panes one peer mirrors, and the queue of what it has not been sent."

    KEEP: ClassVar[dict[str, Keep]] = {
        # The peer reconnects to the next server and mirrors again.
        "connection": Keep.DROPPED,
        "sources": Keep.DROPPED,
        "_queue": Keep.DROPPED,
        "_ready": Keep.DROPPED,
        "terminal": Keep.DROPPED,
        "last_used": Keep.DROPPED,
        "version": Keep.DROPPED,
        "watching": Keep.DROPPED,
        "_tree_changed": Keep.DROPPED,
        "_tree_sent": Keep.DROPPED,
        "_answer_owed": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection, offered: object, version: int) -> None:
        self.connection = connection
        #: The version of the link both sides speak (`peer_version`).
        self.version = version
        self.sources: dict[PaneId, MirrorSource] = {}
        #: What the terminals at the peer say, once it said.
        self.terminal: PeerTerminal | None = None
        #: When a person at the peer last used a mirror, by the
        #: server's count of uses (`Pymux.peer_was_used`).
        self.last_used = 0
        #: The peer asked for the sessions (`watch`).
        self.watching = False
        self._tree_changed = anyio.Event()
        self._tree_sent: Tree | None = None
        self._answer_owed = False
        # Each packet, or the mirror whose frame is built when its turn
        # comes, and whether the stream turns to zstd after it.
        self._queue: deque[tuple[MirrorSource | bytes, bool]] = deque()
        self._ready = anyio.Event()

        # The `wire` packet goes first in this queue, so nothing encoded
        # after the turn can be written before it.
        wire = choose(offered, connection.pipe_connection.can_compress)
        if wire is not None and connection.encode is encode_json:
            self._queue.append((encode_json({Field.CMD: Packet.WIRE, Field.WIRE: wire}), wire == MSGPACK_ZSTD))
            connection.encode = encode_msgpack
        # The peer checks this side's version against its own before it
        # reads anything else.
        self.queue({Field.CMD: Packet.PEER_ACCEPTED, Field.VERSION: PEER_VERSION, Field.OLDEST: OLDEST_PEER_VERSION})

    def queue(self, packet: Message) -> None:
        "Encode a packet now and queue it behind the others."
        self._queue.append((self.connection.encode(packet), False))
        self._ready.set()

    def queue_frame(self, source: MirrorSource) -> None:
        "Queue a frame of `source`, built when the writer reaches it."
        self._queue.append((source, False))
        self._ready.set()

    async def ping(self) -> None:
        "One ping, behind what is queued, so it never jumps the `wire` packet (`ServerConnection.watch_liveness`)."
        self.queue({Field.CMD: Packet.PING})

    async def run(self) -> None:
        "Write what is queued until the connection goes, then let every pane go."
        if self.version >= PEER_PINGS:
            # A peer whose machine slept leaves this link a ghost that
            # still sizes the panes it watched. Lillecarl/pymux#877.
            self.connection.spawn(self.connection.watch_liveness(self.ping))
        try:
            while not self.connection.closed:
                if not self._queue:
                    await self._ready.wait()
                    self._ready = anyio.Event()
                    continue
                entry, compress_after = self._queue.popleft()
                if isinstance(entry, MirrorSource):
                    encoded = self._frame_of(entry)
                    if encoded is None:
                        continue
                else:
                    encoded = entry
                await self.connection.write_encoded(encoded, compress_after)
        except BrokenPipeError:
            pass
        finally:
            logger.info("A peer link ends, after mirroring %d panes.", len(self.sources))
            self.close()

    def _frame_of(self, source: MirrorSource) -> bytes | None:
        if self.sources.get(source.pane.pane_id) is not source:
            return None
        try:
            packet = source.frame()
            return None if packet is None else self.connection.encode(packet)
        except Exception:
            # One pane whose page cannot be sent ends its own mirror, not the link.
            logger.exception("Sending pane %s to a peer failed.", source.pane.pane_id)
            self.lost(source)
            return None

    def pane_said(self, pane: Pane, code: str, param: str) -> None:
        "Pass an OSC sequence of a pane on to the peer (`Outward.forward_osc`)."
        if not self.watching and pane.pane_id not in self.sources:
            return
        # A frame carries the pointer, and the marks of a shell name
        # rows of the history, which stays here.
        if code in (Osc.POINTER_SHAPE, Osc.SHELL_INTEGRATION):
            return
        self.queue(
            {Field.CMD: Packet.MIRROR_OSC, Field.PANE_ID: int(pane.pane_id), Field.CODE: code, Field.DATA: param}
        )

    def tree_changed(self) -> None:
        "Something the tree holds may have changed (`Pymux.invalidate`)."
        self._tree_changed.set()

    def answer(self) -> None:
        """
        Send the tree next though nothing in it changed: the peer did
        something here and holds what it did as a preview until the tree
        says what came of it. A command that changed nothing here still
        has to undo a preview there. Lillecarl/pymux#873.
        """
        self._answer_owed = True
        self._tree_changed.set()

    async def _send_trees(self) -> None:
        "Send the tree, and again after each change to it, until the connection goes."
        pymux = self.connection.pymux
        server = socket.gethostname().partition(".")[0]
        while not self.connection.closed:
            tree = tree_of(pymux)
            owed, self._answer_owed = self._answer_owed, False
            if owed or tree != self._tree_sent:
                self._size_the_panes()
                self._tree_sent = tree
                self.queue(
                    {Field.CMD: Packet.TREE, Field.SERVER: server, Field.SERVER_ID: pymux.server_id, Field.ROWS: tree}
                )
            with anyio.move_on_after(TREE_EVERY):
                await self._tree_changed.wait()
            self._tree_changed = anyio.Event()
            await anyio.sleep(TREE_SETTLE)

    def close(self) -> None:
        for source in self.sources.values():
            source.stop()
        viewed = bool(self.sources)
        self.sources.clear()
        self._queue.clear()
        self._ready.set()
        self._sized({})
        if viewed:
            self.connection.pymux.terminals.sync_viewers()
            self.connection.pymux.focus.sync_panes()

    def lost(self, source: MirrorSource) -> None:
        "A mirror this side could not keep sending: it ends, and the peer hears why."
        self._drop(source)
        self.connection.send_packet(
            {
                Field.CMD: Packet.MIRROR_REFUSED,
                Field.PANE_ID: int(source.pane.pane_id),
                Field.ERROR: "sending it failed",
            }
        )

    def _drop(self, source: MirrorSource) -> None:
        source.stop()
        self.sources.pop(source.pane.pane_id, None)
        if source.focused:
            self.connection.pymux.focus.sync_panes()
        if not self.sources:
            # The peer watches nothing here: its terminals stop counting.
            self.connection.pymux.terminals.sync_viewers()

    def handle(self, packet: Message) -> None:
        _PACKETS.handle(self, packet)

    def _source_of(self, packet: Message) -> MirrorSource | None:
        return self.sources.get(PaneId(int(packet.get(Field.PANE_ID, -1))))

    @_PACKETS.handles(Packet.MIRROR)
    def _on_mirror(self, packet: Message) -> None:
        pane_id = PaneId(int(packet.get(Field.PANE_ID, -1)))
        if pane_id in self.sources:
            return
        pane = self.connection.pymux.panes_by_id.get(pane_id)
        terminal = pane.terminal if pane is not None else None
        if pane is None or not isinstance(terminal, Terminal):
            # A window pane has no screen to lend. Lillecarl/pymux#620.
            self.connection.send_packet(
                {Field.CMD: Packet.MIRROR_REFUSED, Field.PANE_ID: int(pane_id), Field.ERROR: "no such pane"}
            )
            return
        first = not self.sources
        source = self.sources[pane_id] = MirrorSource(self, pane, terminal.terminal_control)
        # A person there asked for it, so the peer's terminal counts as
        # the one used last. The screen this sends then carries it.
        self.connection.pymux.peer_was_used(self)
        if first:
            self.connection.pymux.terminals.sync_viewers()
        source.start()

    @_PACKETS.handles(Packet.MIRROR_PAUSE)
    def _on_pause(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.pause()

    @_PACKETS.handles(Packet.MIRROR_RESUME)
    def _on_resume(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.resume()

    @_PACKETS.handles(Packet.MIRROR_STOP)
    def _on_stop(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            self._drop(source)

    def _driven(self, packet: Message) -> MirrorSource | None:
        "The mirror a person at the peer drives, who is the person who used this server last now."
        source = self._source_of(packet)
        if source is not None:
            self.connection.pymux.peer_was_used(self)
        return source

    @_PACKETS.handles(Packet.MIRROR_KEY)
    def _on_key(self, packet: Message) -> None:
        source = self._driven(packet)
        if source is None:
            return
        data = str(packet.get(Field.DATA, ""))
        if source.copying is not None:
            source.copying.type(data)
        else:
            source.control.type_key(data, _key_profile(packet.get(Field.KEY_PROFILE)))

    @_PACKETS.handles(Packet.MIRROR_PASTE)
    def _on_paste(self, packet: Message) -> None:
        source = self._driven(packet)
        if source is None:
            return
        text = str(packet.get(Field.DATA, ""))
        if source.copying is not None:
            source.copying.paste(text)
        else:
            source.control.paste(text)

    @_PACKETS.handles(Packet.MIRROR_WRITE)
    def _on_write(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            # A pane this server mirrors too sends them on as they came.
            source.control.process.write_input(str(packet.get(Field.DATA, "")))

    @_PACKETS.handles(Packet.MIRROR_MOUSE)
    def _on_mouse(self, packet: Message) -> None:
        source = self._driven(packet)
        mouse = packet.get(Field.MOUSE) or {}
        try:
            action = MouseAction(int(mouse.get("action", 0)))
        except ValueError:
            return
        if source is None:
            return
        column, row = int(mouse.get("column", 0)), int(mouse.get("row", 0))
        if source.copying is not None:
            source.copying.mouse(action, column, row)
        elif not source.control.use_mouse(action, column, row) and action is MouseAction.WHEEL_UP:
            # The wheel a program does not want reads the history, as
            # it does for a client here (`TerminalControl._read_the_history`).
            source.copy(scroll=True)

    @_PACKETS.handles(Packet.MIRROR_COPY)
    def _on_copy(self, packet: Message) -> None:
        source = self._driven(packet)
        if source is None:
            return
        at = place_of(packet.get(Field.COPYING))
        if source.control.mirror is not None:
            # A pane this server mirrors too: the history is further on.
            source.control.mirror.copy_mode(at)
        else:
            source.copy(at=at)

    def copied(self, app: object, text: str) -> bool:
        "Send what a person copied to the peer, when `app` is the copy mode of one of its mirrors (`Outward.copied_in_copy_mode`)."
        for source in self.sources.values():
            if source.copying is not None and source.copying.app is app:
                self.queue({Field.CMD: Packet.MIRROR_COPIED, Field.PANE_ID: int(source.pane.pane_id), Field.DATA: text})
                return True
        return False

    @_PACKETS.handles(Packet.PEER_SIZES)
    def _on_sizes(self, packet: Message) -> None:
        self._sized(
            {
                WindowId(int(window_id)): Size(rows=int(rows), columns=int(columns))
                for window_id, rows, columns in packet.get(Field.SIZES) or ()
            }
        )

    def _sized(self, sizes: dict[WindowId, Size]) -> None:
        "Take the peer's room for each window it looks at (`Planes.peer_rooms`), and lay out the windows that changed."
        pymux = self.connection.pymux
        rooms = pymux.planes.peer_rooms
        before = rooms.get(self, {})
        changed = {window_id for window_id in set(sizes) | set(before) if sizes.get(window_id) != before.get(window_id)}
        self.answer()
        if not changed:
            return
        if sizes:
            rooms[self] = sizes
        else:
            rooms.pop(self, None)
        watched = {window_id for room in rooms.values() for window_id in room}
        for session in pymux.sessions:
            for window in session.arrangement.windows:
                if window.window_id in changed:
                    window.peer_watched = window.window_id in watched
                    if window.peer_watched:
                        # A person there looks at it now, so its marks are seen.
                        window.seen()
        self._size_the_panes(changed)
        pymux.invalidate(Woke.PEER_RESIZED)

    def _size_the_panes(self, window_ids: set[WindowId] | None = None) -> None:
        """
        Give the panes of the windows the peer sizes (or of `window_ids`)
        the boxes their plan gives them. A window nobody here watches
        gets no frame to do it, so a split, a new pane or a size the peer
        sent would keep the size a pane was born with.
        """
        pymux = self.connection.pymux
        wanted = set(pymux.planes.peer_rooms.get(self, {})) if window_ids is None else window_ids
        for session in pymux.sessions:
            for window in session.arrangement.windows:
                if window.window_id in wanted:
                    size_the_panes_of(pymux, window)

    @_PACKETS.handles(Packet.WATCH)
    def _on_watch(self, packet: Message) -> None:
        if not self.watching:
            self.watching = True
            self.connection.spawn(self._send_trees())

    @_PACKETS.handles(Packet.MIRROR_FOCUS)
    def _on_focus(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            source.focused = bool(packet.get(Field.FOCUSED))
            self.connection.pymux.focus.sync_panes()

    @_PACKETS.handles(Packet.PEER_TERMINAL)
    def _on_terminal(self, packet: Message) -> None:
        cell_size = packet.get(Field.CELL_SIZE)
        color_base = packet.get(Field.COLOR_BASE)
        self.terminal = PeerTerminal(
            None if cell_size is None else (int(cell_size[0]), int(cell_size[1])),
            None if color_base is None else color_base_of(color_base),
            int(packet.get(Field.KEYBOARD, 0)),
            str(packet.get(Field.THEME) or DEFAULT_THEME),
        )
        if self.sources:
            self.connection.pymux.terminals.sync_viewers()
