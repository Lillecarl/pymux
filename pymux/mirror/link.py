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

import base64
import socket
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
from libpymux.protocol import (
    OLDEST_PEER_VERSION,
    PEER_VERSION,
    Capability,
    Field,
    Message,
    Packet,
    capabilities_in_common,
    capabilities_named,
)
from libpymux.wire import MSGPACK_ZSTD, choose, encode_json, encode_msgpack
from prompt_toolkit.data_structures import Size
from ptterm import Terminal
from pyte.keep import Keep
from pyte.keys.events import DEFAULT_KEY_PROFILE, KeyProfile
from pyte.mouse import MODIFIERS, MouseAction, MouseBits, MouseButton, MouseInput
from pyte.osc import Osc, color_base_of
from pyte.sequences.kitty_clipboard import b64

from ..connection.log_asks import LogAsks, log_answer
from ..connection.packet_table import PacketTable
from ..connection.recording import recordings_directory
from ..enums import Woke
from ..ids import PaneId, WindowId
from ..layout.geometry import size_the_panes_of
from ..log import logger
from ..pipes.base import BrokenPipeError
from ..style.style import DEFAULT_THEME
from ..terminals import PeerTerminal
from ..wayland.window_pane import WindowWidget
from .copying import place_of
from .files import LinkFiles
from .plugin_messages import PluginAsks, plugin_answer
from .source import MirrorSource
from .tree import Tree, tree_of
from .window_source import MirrorWindowSource

if TYPE_CHECKING:
    from pyte.kitty_clipboard import ClipboardWrite
    from pyte.sequences.kitty_dnd import DndPacket

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
        "windows": Keep.DROPPED,
        "_queue": Keep.DROPPED,
        "_ready": Keep.DROPPED,
        "terminal": Keep.DROPPED,
        "last_used": Keep.DROPPED,
        "version": Keep.DROPPED,
        "capabilities": Keep.DROPPED,
        "watching": Keep.DROPPED,
        "_tree_changed": Keep.DROPPED,
        "_tree_sent": Keep.DROPPED,
        "_answer_owed": Keep.DROPPED,
        "_program_status_sent": Keep.DROPPED,
        "host": Keep.DROPPED,
        "log_asks": Keep.DROPPED,
        "plugin_asks": Keep.DROPPED,
        "files": Keep.DROPPED,
        # The peer reports its table again on the next link.
        "forwards": Keep.DROPPED,
    }

    def __init__(
        self,
        connection: ServerConnection,
        offered: object,
        version: int,
        host: str = "",
        capabilities: frozenset[Capability] | None = None,
    ) -> None:
        self.connection = connection
        #: The machine of the peer, as its `peer` said; "" from one without `Capability.LOGS`.
        self.host = host
        #: The `show-log -p` requests that wait for the peer. Lillecarl/pyterm#936.
        self.log_asks = LogAsks(self.queue)
        #: The plugin messages that wait for the peer. Lillecarl/pyterm#988.
        self.plugin_asks = PluginAsks(self.queue)
        #: The `pymux receive` requests that wait for the peer's client. Lillecarl/pyterm#949.
        self.files = LinkFiles(self.queue)
        #: What the peer forwards for this server, as it last reported
        #: (`Capability.FORWARDS`). Lillecarl/pyterm#976.
        self.forwards: list[dict[str, Any]] = []
        #: The version of the link both sides speak (`peer_version`).
        self.version = version
        #: What both sides named, or what `version` implies when the
        #: peer named nothing (`capabilities_in_common`).
        self.capabilities = capabilities if capabilities is not None else capabilities_in_common(version, None)
        self.sources: dict[PaneId, MirrorSource] = {}
        #: The window panes the peer mirrors. Lillecarl/pyterm#890.
        self.windows: dict[PaneId, MirrorWindowSource] = {}
        #: What the terminals at the peer say, once it said.
        self.terminal: PeerTerminal | None = None
        #: When a person at the peer last used a mirror, by the
        #: server's count of uses (`Pymux.peer_was_used`).
        self.last_used = 0
        #: The peer asked for the sessions (`watch`).
        self.watching = False
        self._tree_changed = anyio.Event()
        self._tree_sent: tuple[Tree, list[tuple[int, int, int]]] | None = None
        self._answer_owed = False
        #: The version of each pane's records this peer was sent.
        self._program_status_sent: dict[PaneId, int] = {}
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
        self.queue(
            {
                Field.CMD: Packet.PEER_ACCEPTED,
                Field.VERSION: PEER_VERSION,
                Field.OLDEST: OLDEST_PEER_VERSION,
                Field.CAPABILITIES: capabilities_named(),
            }
        )

    def can(self, capability: Capability) -> bool:
        "Whether the link carries `capability`."
        return capability in self.capabilities

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
        if self.can(Capability.PINGS):
            # A peer whose machine slept leaves this link a ghost that
            # still sizes the panes it watched. Lillecarl/pyterm#879.
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

    def hears(self, pane: Pane) -> bool:
        "Whether the OSC sequences of `pane` go to this peer."
        return self.watching or pane.pane_id in self.sources

    def pane_said(self, pane: Pane, code: str, param: str, acts: bool) -> None:
        """
        Pass an OSC sequence of a pane on to the peer (`Outward.forward_osc`).
        `acts` says whether the peer opens its URL or saves its download.
        """
        if not self.hears(pane):
            return
        # A prompt ends the work in progress, so both change the records.
        if code in (Osc.SHELL_INTEGRATION, Osc.PROGRAM_STATUS):
            self.send_program_status(pane)
        # A frame carries the pointer, and the marks of a shell name
        # rows of the history, which stays here.
        if code in (Osc.POINTER_SHAPE, Osc.SHELL_INTEGRATION, Osc.PROGRAM_STATUS):
            return
        packet: Message = {
            Field.CMD: Packet.MIRROR_OSC,
            Field.PANE_ID: int(pane.pane_id),
            Field.CODE: code,
            Field.DATA: param,
        }
        if code == Osc.TERMINAL_EXTENSION and self.can(Capability.ACTS):
            packet[Field.ACTS] = acts
        self.queue(packet)

    def send_program_status(self, pane: Pane) -> None:
        """
        Send the OSC 7501 records of `pane` whole, when they changed since
        the last send. Not the reports one by one: a prompt or the end of
        the process drops records with no report. Lillecarl/pyterm#930.
        """
        statuses = getattr(pane.screen, "program_status", None)
        if not self.can(Capability.PROGRAM_STATUS) or statuses is None or not self.hears(pane):
            return
        if self._program_status_sent.get(pane.pane_id, 0) == statuses.version:
            return
        self._program_status_sent[pane.pane_id] = statuses.version
        self.queue(
            {
                Field.CMD: Packet.MIRROR_PROGRAM_STATUS,
                Field.PANE_ID: int(pane.pane_id),
                Field.DATA: [record.payload() for record in statuses.records.values()],
            }
        )

    def pane_wrote(self, pane: Pane, write: ClipboardWrite, text: str | None) -> None:
        """
        Pass a pane's kitty clipboard write on to the peer
        (`RichClipboard.serve_write`). A peer without
        `Capability.RICH_CLIPBOARD` gets `text`, the OSC 52 of its text, if any.
        """
        if not self.can(Capability.RICH_CLIPBOARD):
            if text is not None:
                self.pane_said(pane, Osc.CLIPBOARD, text, acts=False)
            return
        if not self.hears(pane):
            return
        self.queue(
            {
                Field.CMD: Packet.MIRROR_CLIPBOARD_WRITE,
                Field.PANE_ID: int(pane.pane_id),
                Field.DATA: {mime: b64(content) for mime, content in write.data.items()},
                Field.ALIASES: dict(write.aliases),
                Field.PRIMARY: write.primary,
                Field.NAME: write.name,
                Field.PASSWORD: write.password,
            }
        )

    def pane_reads(self, pane: Pane, sequence: str) -> None:
        "Ask the peer's terminal for its clipboard, for `pane` (`RichClipboard._read`)."
        self.queue({Field.CMD: Packet.MIRROR_CLIPBOARD_READ, Field.PANE_ID: int(pane.pane_id), Field.DATA: sequence})

    def pane_dnd(self, pane: Pane, packet: DndPacket) -> None:
        "A drag and drop packet of a pane's program, for the drop or drag a terminal at the peer carries."
        if self.hears(pane):
            self.queue({Field.CMD: Packet.MIRROR_DND, Field.PANE_ID: int(pane.pane_id), Field.DATA: packet.param()})

    @_PACKETS.handles(Packet.MIRROR_DND)
    def _on_dnd(self, packet: Message) -> None:
        source = self._driven(packet)
        if source is not None:
            self.connection.pymux.drag_and_drop.peer_said(self, source.pane, str(packet.get(Field.DATA, "")))

    def pane_transfer(self, pane: Pane, param: str) -> None:
        "One OSC 5113 command of a pane's program, for the session a terminal at the peer answers."
        if self.hears(pane):
            self.queue({Field.CMD: Packet.MIRROR_KITTY_TRANSFER, Field.PANE_ID: int(pane.pane_id), Field.DATA: param})

    @_PACKETS.handles(Packet.MIRROR_KITTY_TRANSFER)
    def _on_kitty_transfer(self, packet: Message) -> None:
        if self._driven(packet) is not None:
            self.connection.pymux.kitty_transfers.terminal_said(self.connection, str(packet.get(Field.DATA, "")))

    @_PACKETS.handles(Packet.MIRROR_KITTY_TRANSFER_TAKEN)
    def _on_kitty_transfer_taken(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            self.connection.spawn(
                self.connection.pymux.kitty_transfers.taken_for_peer(self, source.pane, str(packet.get(Field.DATA, "")))
            )

    def pane_took(self, pane: Pane, session_id: str) -> None:
        "Say the pane's program took what the peer's session sent it."
        self.queue(
            {Field.CMD: Packet.MIRROR_KITTY_TRANSFER_TAKEN, Field.PANE_ID: int(pane.pane_id), Field.DATA: session_id}
        )

    @_PACKETS.handles(Packet.MIRROR_CLIPBOARD_ANSWER)
    def _on_clipboard_answer(self, packet: Message) -> None:
        self.connection.pymux.rich_clipboard.peer_answered(self, str(packet.get(Field.DATA, "")))

    @_PACKETS.handles(Packet.MIRROR_CLIPBOARD_PASSWORD)
    def _on_clipboard_password(self, packet: Message) -> None:
        self.connection.pymux.rich_clipboard.peer_password(self, str(packet.get(Field.DATA, "")))

    def tree_changed(self) -> None:
        "Something the tree holds may have changed (`Pymux.invalidate`)."
        self._tree_changed.set()

    def answer(self) -> None:
        """
        Send the tree next though nothing in it changed: the peer did
        something here and holds what it did as a preview until the tree
        says what came of it. A command that changed nothing here still
        has to undo a preview there. Lillecarl/pyterm#875.
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
            # The size each window has here, which the peer lays it out
            # at. A peer of an older build ignores it. Lillecarl/pyterm#932.
            sizes = sorted(
                {
                    (int(window.window_id), size.rows, size.columns)
                    for session in pymux.sessions
                    for window in session.arrangement.windows
                    for size in (pymux.planes.size(window),)
                }
            )
            if owed or (tree, sizes) != self._tree_sent:
                self._size_the_panes()
                self._tree_sent = (tree, sizes)
                self.queue(
                    {
                        Field.CMD: Packet.TREE,
                        Field.SERVER: server,
                        Field.SERVER_ID: pymux.server_id,
                        Field.ROWS: tree,
                        Field.SIZES: [list(size) for size in sizes],
                    }
                )
                # After the tree, so the peer has the panes to put them on.
                for pane in list(pymux.panes_by_id.values()):
                    self.send_program_status(pane)
            with anyio.move_on_after(TREE_EVERY):
                await self._tree_changed.wait()
            self._tree_changed = anyio.Event()
            await anyio.sleep(TREE_SETTLE)

    def close(self) -> None:
        for source in self.sources.values():
            source.stop()
        for window in self.windows.values():
            window.stop()
        self.windows.clear()
        viewed = bool(self.sources)
        self.sources.clear()
        self._queue.clear()
        self._ready.set()
        self._sized({})
        self.log_asks.gone()
        self.plugin_asks.gone()
        self.files.gone()
        self.connection.pymux.drag_and_drop.link_gone(self)
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

    @_PACKETS.handles(Packet.REPORT_LOG)
    def _on_report_log(self, packet: Message) -> None:
        async def answer() -> None:
            self.queue(await anyio.to_thread.run_sync(log_answer, packet))

        self.connection.spawn(answer())

    @_PACKETS.handles(Packet.LOG)
    def _on_log(self, packet: Message) -> None:
        self.log_asks.answered(packet)

    @_PACKETS.handles(Packet.PLUGIN_ASK)
    def _on_plugin_ask(self, packet: Message) -> None:
        async def reply() -> None:
            self.queue(await plugin_answer(self.connection.pymux, packet, self.host, dialled=False))

        self.connection.spawn(reply())

    @_PACKETS.handles(Packet.PLUGIN_ANSWER)
    def _on_plugin_answer(self, packet: Message) -> None:
        self.plugin_asks.answered(packet)

    @_PACKETS.handles(Packet.PEER_UPLOAD)
    def _on_upload(self, packet: Message) -> None:
        self.files.took_part(packet)

    @_PACKETS.handles(Packet.PEER_FILES_LISTED)
    def _on_files_listed(self, packet: Message) -> None:
        self.files.listed(packet)

    @_PACKETS.handles(Packet.MIRROR_CLIPBOARD_IMAGE)
    def _on_clipboard_image(self, packet: Message) -> None:
        "The image a person's Ctrl-V brings, for this server's compositor to offer. Lillecarl/pyterm#958."
        try:
            image = base64.b64decode(str(packet.get(Field.DATA) or ""), validate=True) or None
        except ValueError:
            logger.warning("Dropped a mirrored clipboard image that is not base64.")
            return
        self.connection.pymux.wayland.hold_image(image)

    @property
    def forwards_id(self) -> str:
        "What the `forwards` table names this peer by, where a client is named by its id."
        return "peer:%s" % (self.host,)

    @_PACKETS.handles(Packet.FORWARDS)
    def _on_forwards(self, packet: Message) -> None:
        """
        What the peer forwards for this server, after it changed or the
        link came back: a copy, as a client's is. Lillecarl/pyterm#976.
        """
        reported = packet.get(Field.DATA)
        self.forwards = reported if isinstance(reported, list) else []
        said = packet.get(Field.MESSAGE)
        if said:
            logger.info("The peer on %s: %s", self.host or "a machine", said)
        self.connection.pymux.invalidate(Woke.FORWARDS_CHANGED)

    @_PACKETS.handles(Packet.RECORDING)
    def _on_recording(self, packet: Message) -> None:
        """
        A piece of a client recording the peer made on a session of this
        server: it goes where this server's own recordings go, beside a
        `.part` until its last piece. Lillecarl/pyterm#957.
        """
        name = str(packet.get(Field.NAME) or "")
        # Only a file name: a peer writes nowhere but the recordings.
        if not name or Path(name).name != name or name.startswith("."):
            logger.warning("Dropped a recording piece named %r.", name)
            return
        try:
            piece = base64.b64decode(str(packet.get(Field.DATA) or ""), validate=True)
        except ValueError:
            logger.warning("Dropped a piece of the recording %s that is not base64.", name)
            return
        directory = recordings_directory()
        part = directory / (name + ".part")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # The first piece starts the file: a `.part` left by a link
            # that went mid-file is not part of this one.
            with open(part, "wb" if not packet.get(Field.OFFSET) else "ab") as held:
                held.write(piece)
            if packet.get(Field.LAST):
                part.replace(directory / name)
                logger.info("Recorded %s from the peer on %s.", directory / name, self.host or "a peer")
        except OSError as error:
            logger.warning("Could not write the recording %s: %s", name, error)

    @_PACKETS.handles(Packet.MIRROR)
    def _on_mirror(self, packet: Message) -> None:
        pane_id = PaneId(int(packet.get(Field.PANE_ID, -1)))
        if pane_id in self.sources:
            return
        pane = self.connection.pymux.panes_by_id.get(pane_id)
        terminal = pane.terminal if pane is not None else None
        if pane is not None and isinstance(terminal, WindowWidget):
            self._mirror_window(pane, terminal, packet)
            return
        if pane is None or not isinstance(terminal, Terminal):
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
        self.send_program_status(pane)

    def _mirror_window(self, pane: Pane, widget: WindowWidget, packet: Message) -> None:
        "Lend a window pane: its frames as pixels. Lillecarl/pyterm#890."
        pane_id = pane.pane_id
        if pane_id in self.windows:
            return
        error = None
        if not self.can(Capability.WINDOWS):
            error = "it shows a Wayland window, and the server mirroring it needs an upgrade to show one"
        elif widget.host is not self.connection.pymux.wayland:
            error = "it shows a Wayland window that this server mirrors itself"
        if error is not None:
            self.connection.send_packet(
                {Field.CMD: Packet.MIRROR_REFUSED, Field.PANE_ID: int(pane_id), Field.ERROR: error}
            )
            return
        codecs = frozenset(str(each) for each in packet.get(Field.CODECS) or ())
        window = self.windows[pane_id] = MirrorWindowSource(self, pane, widget, codecs)
        self.connection.pymux.peer_was_used(self)
        window.start()

    def window_drew(self, pane: Pane) -> None:
        "A window pane drew a frame: send it on if the peer mirrors it (`Panes.window_pane_drew`)."
        window = self.windows.get(pane.pane_id)
        if window is not None:
            window.drew()

    def _window_of(self, packet: Message) -> MirrorWindowSource | None:
        return self.windows.get(PaneId(int(packet.get(Field.PANE_ID, -1))))

    @_PACKETS.handles(Packet.MIRROR_WINDOW_SHOWN)
    def _on_window_shown(self, packet: Message) -> None:
        window = self._window_of(packet)
        if window is not None:
            window.taken()

    @_PACKETS.handles(Packet.MIRROR_WINDOW_INPUT)
    def _on_window_input(self, packet: Message) -> None:
        window = self._window_of(packet)
        data = packet.get(Field.DATA)
        if window is not None and isinstance(data, dict):
            self.connection.pymux.peer_was_used(self)
            window.input(data)

    @_PACKETS.handles(Packet.MIRROR_PAUSE)
    def _on_pause(self, packet: Message) -> None:
        source = self._source_of(packet) or self._window_of(packet)
        if source is not None:
            source.pause()

    @_PACKETS.handles(Packet.MIRROR_RESUME)
    def _on_resume(self, packet: Message) -> None:
        source = self._source_of(packet) or self._window_of(packet)
        if source is not None:
            source.resume()

    @_PACKETS.handles(Packet.MIRROR_STOP)
    def _on_stop(self, packet: Message) -> None:
        source = self._source_of(packet)
        if source is not None:
            self._drop(source)
        window = self.windows.pop(PaneId(int(packet.get(Field.PANE_ID, -1))), None)
        if window is not None:
            window.stop()

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
        fields = packet.get(Field.MOUSE) or {}
        try:
            mouse = MouseInput(
                MouseAction(int(fields.get("action", 0))),
                int(fields.get("column", 0)),
                int(fields.get("row", 0)),
                MouseButton(int(fields.get("button", MouseButton.LEFT))),
                MouseBits(int(fields.get("modifiers", 0))) & MODIFIERS,
                min(max(float(fields.get("across", 0.5)), 0.0), 1.0),
                min(max(float(fields.get("down", 0.5)), 0.0), 1.0),
            )
        except TypeError, ValueError:
            return
        if source is None:
            return
        if source.copying is not None:
            source.copying.mouse(mouse)
        elif not source.control.use_mouse(mouse) and mouse.action is MouseAction.WHEEL_UP:
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
