"""
The peer's side of a peer link: this server, holding mirrors of panes
another server owns.

One link per server, whatever number of its panes are mirrored here.
`link.py` is the other end.
"""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, cast

import anyio
from libpymux.connection import CommandResult, Reply, command_packet
from libpymux.protocol import OLDEST_PEER_VERSION, PEER_VERSION, Field, Message, Packet, peer_upgrade
from libpymux.wire import MSGPACK, MSGPACK_ZSTD, PacketReader, encode_json
from prompt_toolkit.application.current import set_app
from prompt_toolkit.clipboard import ClipboardData
from pyte.keep import Keep
from pyte.kitty_clipboard import ClipboardWrite
from pyte.osc import color_base_data
from pyte.sequences.file_transfer import Malformed as TransferMalformed
from pyte.sequences.file_transfer import TransferCommand
from pyte.sequences.kitty_clipboard import strict_b64decode
from pyte.sequences.kitty_dnd import DndPacket, Malformed

from ..client.defaults import is_ssh_url
from ..client.reconnect import link_ended_notice, link_notice, why
from ..client.relink import Relink, Restarting, Wait
from ..commands import CommandException
from ..enums import Clipboard, Woke
from ..graphics.kitty_frames import decoders
from ..log import logger
from ..options.tables import PeerCopyRow, PeerPlaceRow
from ..wayland.window_pane import WindowWidget
from .copying import place_of
from .dial import abort, dial, dial_beside
from .program import HOLD_FOR_FRESH, RemoteProgram
from .tree import MirrorTree
from .tunnel import Tunnels

if TYPE_CHECKING:
    from collections.abc import Callable

    from prompt_toolkit.data_structures import Size
    from pyte.keys.events import KeyProfile
    from pyte.mouse import MouseInput

    from ..connection.client_state import ClientState
    from ..core.main import Pymux
    from ..core.pane import Pane
    from ..core.session import Session
    from ..core.window_tree import Window
    from ..terminals import PeerTerminal
    from .copying import Place
    from .link import PeerLink
    from .sink import MirrorSink
    from .window import RemoteWindow

__all__ = ["Peer", "Peers", "Told"]


def _clipboard_write_of(packet: Message) -> ClipboardWrite | None:
    "The write a `mirror-clipboard-write` carries, or None for one that does not decode."
    data, aliases = packet.get(Field.DATA), packet.get(Field.ALIASES) or {}
    if not isinstance(data, dict) or not isinstance(aliases, dict):
        return None
    try:
        decoded = {str(mime): strict_b64decode(str(content)) for mime, content in data.items()}
    except ValueError:
        logger.warning("Dropped a mirrored clipboard write whose data is not base64.")
        return None
    return ClipboardWrite(
        decoded,
        {str(alias): str(target) for alias, target in aliases.items()},
        primary=bool(packet.get(Field.PRIMARY)),
        password=str(packet.get(Field.PASSWORD) or ""),
        name=str(packet.get(Field.NAME) or ""),
    )


class Peers:
    "This server's links to other servers, one per address."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # A link ends with the server; the snapshot holds its address
        # in `peers`, and a load reconnects (`reconnect`).
        "by_address": Keep.DROPPED,
        "returning": Keep.DROPPED,
        "returning_copies": Keep.DROPPED,
        "returning_places": Keep.DROPPED,
        "tunnels": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.by_address: dict[str, Peer] = {}
        #: The addresses a loaded snapshot was linked to.
        self.returning: list[str] = []
        #: Each mirror a loaded snapshot was in copy mode on (`copy_rows`).
        self.returning_copies: list[PeerCopyRow] = []
        #: Each client a loaded snapshot had on a mirrored session (`place_rows`).
        self.returning_places: list[PeerPlaceRow] = []
        #: The ports here for loopback URLs of mirrored panes.
        self.tunnels = Tunnels(pymux)

    def reconnect(self) -> None:
        """
        Link again to every server a loaded snapshot was linked to. The
        owners send their trees again, so nothing of theirs is saved.
        """
        returning = self.returning
        self.returning = []
        for address in returning:
            self.of(address).watch()
        for copy in self.returning_copies:
            peer = self.by_address.get(copy.address)
            place = place_of((copy.caret, copy.selection_start, copy.selection_kind))
            if peer is not None and place is not None:
                peer.copies[copy.pane_id] = place
        self.returning_copies = []
        until = anyio.current_time() + PLACE_KEPT
        for row in self.returning_places:
            peer = self.by_address.get(row.address)
            if peer is not None:
                peer.places[row.client_id] = ReturningPlace(row.session_id, row.window_id, until)
        self.returning_places = []

    def place_rows(self) -> list[PeerPlaceRow]:
        "Each client on a mirrored session, by its id, with the link and the owner's session and window."
        found = []
        for client_state in self.pymux._client_states.values():
            client_id = client_state.client.client_id
            if client_state.temporary or not client_id:
                continue
            remote = client_state.session.remote
            if remote is None or remote.peer.address not in self.by_address:
                continue
            window = client_state.session.arrangement.get_active_window_for(client_state.app)
            window_id = remote.peer.tree.id_of_window(window) if window is not None else None
            if window_id is not None:
                found.append(PeerPlaceRow(client_id, remote.peer.address, remote.session_id, window_id))
        return found

    def return_clients(self) -> None:
        "Put each returning client back on its mirrored session, once both are here."
        for peer in self.by_address.values():
            peer.return_clients()

    def copy_rows(self) -> list[PeerCopyRow]:
        "Each mirror here in copy mode, by address and the owner's pane id, with where it stands."
        return [
            PeerCopyRow(address, pane_id, *sink.copying)
            for address, peer in self.by_address.items()
            for pane_id, sink in sorted(peer.sinks.items())
            if sink.copying is not None
        ]

    def of(self, address: str, **connect_with: Any) -> Peer:
        "The link to `address`, opened in the server's scope if there is none yet."
        peer = self.by_address.get(address)
        if peer is None:
            peer = self.by_address[address] = Peer(self.pymux, address, **connect_with)
            peer.tell_terminal(self.pymux.terminals.peer_terminal())
        if not peer.running:
            # A link that ended, because its first dial failed or the
            # owner refused it, is tried again when asked for again.
            if self.pymux.tasks is None:
                raise RuntimeError("A peer link was asked for outside `Pymux.running`, so nothing would hold it.")
            peer.running = True
            self.pymux.tasks.start_soon(peer.run)
        return peer

    def sync_sight(self) -> None:
        """
        Tell every mirrored pane whether a client here shows it: the
        panes of the window each client looks at, in a mirrored session.
        And tell each owner how much room those windows have here, so
        it lays them out at that size. `Pymux.invalidate` calls this,
        since a switch, a split and a resize all reach it.
        """
        if not self.by_address:
            return
        seen: dict[int, list[ClientState]] = {}
        windows: list[Window] = []
        for client_state in self.pymux._client_states.values():
            session = client_state.session
            if client_state.temporary or session.remote is None:
                continue
            window = session.arrangement.get_active_window_for(client_state.app)
            if window is not None:
                for pane in window.panes:
                    seen.setdefault(id(pane), []).append(client_state)
                windows.append(window)
        for peer in self.by_address.values():
            for pane in peer.tree.panes.values():
                if isinstance(pane.terminal, WindowWidget):
                    cast("RemoteWindow", pane.terminal.host).seen(id(pane) in seen)
                    continue
                program = cast(RemoteProgram, pane.process)
                if program.seen(id(pane) in seen):
                    # Its page is old: the clients that show it wait for
                    # the frame that catches it up.
                    for client_state in seen[id(pane)]:
                        client_state.frame_holds.hold(
                            ("mirror", pane.pane_id), lambda program=program: not program.stale, HOLD_FOR_FRESH
                        )
            peer.tell_sizes(
                {
                    remote_id: self.pymux.planes.size(window)
                    for remote_id, window in peer.tree.windows.items()
                    if any(window is one for one in windows)
                }
            )


#: How long a client's place on a mirrored session waits, after this
#: server loaded a snapshot, for the client and the owner's tree.
PLACE_KEPT = 30.0


class ReturningPlace:
    "Where a client was on a mirrored session before this server upgraded: the owner's ids."

    def __init__(self, session_id: int, window_id: int, until: float) -> None:
        self.session_id = session_id
        self.window_id = window_id
        self.until = until
        #: The session the client came back on, as first seen. A client
        #: a person moved since stays where they put it.
        self.landed: Session | None = None


class Told:
    """
    What a peer told its owner and still holds, by key: each entry is
    the packet that says it, or what makes that packet when a link
    starts. A new link hears every entry again, in the order each was
    first said, so state added here cannot be left out of a reconnect.
    Lillecarl/pymux#883.
    """

    def __init__(self, send: Callable[[Message], None]) -> None:
        self._send = send
        self._held: dict[tuple[object, ...], Message | Callable[[], Message | None]] = {}

    def say(self, key: tuple[object, ...], packet: Message) -> None:
        "Hold `packet` under `key`, and send it unless it is what was said already."
        if self._held.get(key) == packet:
            return
        self._held[key] = packet
        self._send(packet)

    def hold(self, key: tuple[object, ...], make: Callable[[], Message | None]) -> None:
        "Hold what makes a packet when a link starts, and send nothing now."
        self._held[key] = make

    def holds(self, key: tuple[object, ...]) -> bool:
        return key in self._held

    def again(self, *keys: tuple[object, ...]) -> None:
        """
        Say these again, after everything held: what was said of a pane
        before its mirror reached the owner found nothing there to hear it.
        """
        for key in keys:
            entry = self._held.pop(key, None)
            if entry is not None:
                self._held[key] = entry
                packet = entry() if callable(entry) else entry
                if packet is not None:
                    self._send(packet)

    def forget(self, *keys: tuple[object, ...]) -> None:
        for key in keys:
            self._held.pop(key, None)

    def replay(self) -> None:
        "Say it all again, on a new link."
        for entry in list(self._held.values()):
            packet = entry() if callable(entry) else entry
            if packet is not None:
                self._send(packet)


class _PaneWaiting:
    """
    A peer's wait (`relink.Waiting`): the notice lies over each pane of
    the link, and `connect` on its address dials at once.
    """

    def __init__(self, peer: Peer) -> None:
        self.peer = peer
        self._now: anyio.Event | None = None

    def show(self, said: str, seconds: float, internet: bool | None, trying: bool) -> None:
        self.peer.error = said
        self.peer.say(tuple(link_notice(self.peer.server or self.peer.address, said, seconds, internet, trying)))

    async def wait(self, seconds: float) -> Wait:
        self._now = now = anyio.Event()
        try:
            with anyio.move_on_after(seconds):
                await now.wait()
        finally:
            self._now = None
        return Wait.NOW if now.is_set() else Wait.ON

    def dial_now(self) -> None:
        if self._now is not None:
            self._now.set()

    def done(self) -> None:
        "The notice stays until the first frame of the new link takes it off its pane (`Peer._take`)."


class Peer:
    """
    A link to the server at `address`, and the mirrors on it.

    **A link that goes is dialled again**, unless the owner refused it,
    so a mirror outlives the owner's upgrade. Each link starts from what
    this side holds: the hello, then the watch, the terminal, the sizes
    and each mirror with its pause and its copy mode. A packet sent
    while no link is up is dropped, since the next link says it all.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        # A mirror is rebuilt by its program after an upgrade, and asks
        # the next link for the whole page.
        "address": Keep.REBUILT,
        "connect_with": Keep.REBUILT,
        "sinks": Keep.DROPPED,
        "told": Keep.DROPPED,
        "copies": Keep.DROPPED,
        "connected": Keep.DROPPED,
        "error": Keep.DROPPED,
        "_send": Keep.DROPPED,
        "_receive": Keep.DROPPED,
        "accepted": Keep.DROPPED,
        "stream": Keep.DROPPED,
        "tree": Keep.DROPPED,
        "server": Keep.DROPPED,
        "restart": Keep.DROPPED,
        "places": Keep.DROPPED,
        "running": Keep.DROPPED,
        "relink": Keep.DROPPED,
        "_waiting": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, address: str, **connect_with: Any) -> None:
        self.address = address
        self.connect_with = connect_with
        self.sinks: dict[int, MirrorSink | RemoteWindow] = {}
        #: What this side told the owner and still holds, which every
        #: new link hears again (`_hello`).
        self.told = Told(self.send)
        #: Where copy mode stood on each pane before this server upgraded,
        #: for the first time the pane is mirrored again.
        self.copies: dict[int, Place] = {}
        #: The owner's sessions as built here, once this side watches.
        self.tree = MirrorTree(pymux, self)
        #: Whether the link is up.
        self.connected = False
        #: Why the link went or never came, for the person to read.
        self.error: str | None = None
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)
        #: The link's stream while it is up, which a stream beside it starts from.
        self.stream: anyio.abc.ByteStream | None = None
        #: The owner said which versions of the link it speaks, and they meet ours.
        self.accepted = False
        #: What the owner calls itself, as its trees say.
        self.server: str | None = None
        #: The owner said it restarts: why, and until when on the loop's clock.
        self.restart: tuple[str, float] | None = None
        #: Where each client was on this link's sessions before this
        #: server upgraded, by client id, until it is back there.
        self.places: dict[str, ReturningPlace] = {}
        #: `run` holds or dials the link. It ends when the owner refuses
        #: the link, the first dial fails or a failure cannot come back,
        #: and `Peers.of` starts it again.
        self.running = False
        self._waiting = _PaneWaiting(self)
        #: The link while it is gone, as an SSH client's (`relink`). A
        #: sleep here breaks only a link that crosses the network.
        self.relink: Relink[anyio.abc.ByteStream] = Relink(
            self._waiting,
            dial=lambda timeout: dial(self.address, timeout, **self.connect_with),
            abort=abort,
            watch_sleep=is_ssh_url(address),
        )

    def dial_now(self) -> None:
        "Dial at once when the link is down, rather than at the end of the wait."
        self._waiting.dial_now()

    def return_clients(self) -> None:
        """
        Put each client back where it was on this link's sessions before
        this server upgraded, once the client attached again and the tree
        brought the session. A snapshot holds no mirrored session, so the
        client came back on one of this server's. Lillecarl/pymux#870.
        """
        if not self.places:
            return
        pymux = self.tree.pymux
        now = anyio.current_time()
        by_id = {
            client_state.client.client_id: client_state
            for client_state in pymux._client_states.values()
            if client_state.client.client_id and not client_state.temporary
        }
        for client_id, place in list(self.places.items()):
            client_state = by_id.get(client_id)
            if now > place.until or (client_state is not None and place.landed not in (None, client_state.session)):
                del self.places[client_id]
                continue
            if client_state is None:
                continue
            if place.landed is None:
                place.landed = client_state.session
            session = self.tree.sessions.get(place.session_id)
            if session is None:
                continue
            del self.places[client_id]
            pymux.attach_client_to(client_state, session)
            window = self.tree.windows.get(place.window_id)
            if window is not None and window in session.arrangement.windows:
                with set_app(client_state.app):
                    session.arrangement.set_active_window(window)
            pymux.invalidate(Woke.SESSION_CHANGED)

    def send(self, packet: Message) -> None:
        if self.connected:
            self._send.send_nowait(encode_json(packet))

    def dnd(self, pane_id: int, param: str) -> None:
        "A drag and drop packet from a terminal here, for the program in the owner's pane `pane_id`."
        self.send({Field.CMD: Packet.MIRROR_DND, Field.PANE_ID: pane_id, Field.DATA: param})

    def kitty_transfer(self, pane_id: int, param: str) -> None:
        "One OSC 5113 command from a terminal here, for the program in the owner's pane `pane_id`."
        self.send({Field.CMD: Packet.MIRROR_KITTY_TRANSFER, Field.PANE_ID: pane_id, Field.DATA: param})

    def kitty_transfer_taken(self, pane_id: int, session_id: str) -> None:
        "Ask the owner to say when the program in pane `pane_id` took what session `session_id` sent it."
        self.send({Field.CMD: Packet.MIRROR_KITTY_TRANSFER_TAKEN, Field.PANE_ID: pane_id, Field.DATA: session_id})

    def send_clipboard_answer(self, sequence: str) -> None:
        "One packet of this side's answer to the owner's clipboard read (`RichClipboard.read_for_owner`)."
        self.send({Field.CMD: Packet.MIRROR_CLIPBOARD_ANSWER, Field.DATA: sequence})

    def _hello(self) -> None:
        "Start a new link from what this side holds."
        # Zstd only across a network, as a client offers it.
        offer = [MSGPACK_ZSTD, MSGPACK] if is_ssh_url(self.address) else [MSGPACK]
        self.send(
            {Field.CMD: Packet.PEER, Field.WIRE: offer, Field.VERSION: PEER_VERSION, Field.OLDEST: OLDEST_PEER_VERSION}
        )
        self.told.replay()

    # -- what a mirror asks --------------------------------------------

    def mirror(self, pane_id: int, sink: MirrorSink | RemoteWindow) -> None:
        self.sinks[pane_id] = sink
        # The codecs this side decodes, for a window pane. Lillecarl/pymux#888.
        self.told.say(
            ("mirror", pane_id), {Field.CMD: Packet.MIRROR, Field.PANE_ID: pane_id, Field.CODECS: sorted(decoders())}
        )
        # Where the owner's frames last put copy mode, which is the
        # owner's to move, so a new link reads it when it starts.
        self.told.hold(("copy", pane_id), lambda: self._copy_packet(pane_id, sink.copying) if sink.copying else None)
        self.told.again(("paused", pane_id), ("focus", pane_id))
        place = self.copies.pop(pane_id, None)
        if place is not None:
            sink.copying = place
            self.copy_mode(pane_id, place)

    def stop(self, pane_id: int) -> None:
        self.told.forget(("mirror", pane_id), ("paused", pane_id), ("copy", pane_id), ("focus", pane_id))
        if self.sinks.pop(pane_id, None) is not None:
            self.send({Field.CMD: Packet.MIRROR_STOP, Field.PANE_ID: pane_id})

    def pause(self, pane_id: int) -> None:
        self.told.say(("paused", pane_id), {Field.CMD: Packet.MIRROR_PAUSE, Field.PANE_ID: pane_id})

    def resume(self, pane_id: int) -> None:
        self.told.forget(("paused", pane_id))
        self.send({Field.CMD: Packet.MIRROR_RESUME, Field.PANE_ID: pane_id})

    def key(self, pane_id: int, data: str, profile: KeyProfile) -> None:
        self.send(
            {
                Field.CMD: Packet.MIRROR_KEY,
                Field.PANE_ID: pane_id,
                Field.DATA: data,
                Field.KEY_PROFILE: profile._asdict(),
            }
        )

    def write(self, pane_id: int, data: str) -> None:
        self.send({Field.CMD: Packet.MIRROR_WRITE, Field.PANE_ID: pane_id, Field.DATA: data})

    def paste(self, pane_id: int, text: str) -> None:
        self.send({Field.CMD: Packet.MIRROR_PASTE, Field.PANE_ID: pane_id, Field.DATA: text})

    def mouse(self, pane_id: int, mouse: MouseInput) -> None:
        self.send(
            {
                Field.CMD: Packet.MIRROR_MOUSE,
                Field.PANE_ID: pane_id,
                Field.MOUSE: {
                    "action": int(mouse.action),
                    "column": mouse.column,
                    "row": mouse.row,
                    "button": int(mouse.button),
                    "modifiers": int(mouse.modifiers),
                    "across": mouse.across,
                    "down": mouse.down,
                },
            }
        )

    def copy_mode(self, pane_id: int, at: Place | None = None) -> None:
        self.send(self._copy_packet(pane_id, at))

    def _copy_packet(self, pane_id: int, at: Place | None) -> Message:
        packet: Message = {Field.CMD: Packet.MIRROR_COPY, Field.PANE_ID: pane_id}
        if at is not None:
            packet[Field.COPYING] = list(at)
        return packet

    def tell_sizes(self, sizes: dict[int, Size]) -> None:
        "Tell the owner the room each of its windows has here, when that changed."
        self.told.say(
            ("sizes",),
            {
                Field.CMD: Packet.PEER_SIZES,
                Field.SIZES: [[window_id, size.rows, size.columns] for window_id, size in sorted(sizes.items())],
            },
        )

    @property
    def watching(self) -> bool:
        "This side asked for the owner's sessions."
        return self.told.holds(("watch",))

    def watch(self) -> None:
        "Ask the owner for its sessions, now and on every change."
        self.told.say(("watch",), {Field.CMD: Packet.WATCH})

    def focus(self, pane_id: int, focused: bool) -> None:
        packet: Message = {Field.CMD: Packet.MIRROR_FOCUS, Field.PANE_ID: pane_id, Field.FOCUSED: focused}
        if focused:
            self.told.say(("focus", pane_id), packet)
        else:
            self.told.forget(("focus", pane_id))
            self.send(packet)

    def tell_terminal(self, terminal: PeerTerminal) -> None:
        "Tell the owner what the terminals here say, when it changed (`Terminals.tell_peers`)."
        self.told.say(
            ("terminal",),
            {
                Field.CMD: Packet.PEER_TERMINAL,
                Field.CELL_SIZE: None if terminal.cell_size is None else list(terminal.cell_size),
                Field.COLOR_BASE: None if terminal.color_base is None else color_base_data(terminal.color_base),
                Field.KEYBOARD: terminal.keyboard_source_flags,
                Field.THEME: terminal.theme,
            },
        )

    async def run_command(self, text: str, pane_id: int | None, targets: dict[str, str]) -> CommandResult:
        """
        Run one command on the owner, as a command typed in its pane
        `pane_id` runs there, and read what it says. It goes on a
        stream of its own, because the owner answers a command and
        closes the connection it came on.
        """
        if self.stream is None:
            raise CommandException("the link to %s is down" % (self.address,))
        reply = Reply()
        async with await dial_beside(self.address, self.stream) as stream:
            await stream.send(
                command_packet(text, None if pane_id is None else str(pane_id), None, targets, forwarded=True)
            )
            while True:
                try:
                    data = await stream.receive()
                except anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError:
                    break
                reply.feed(data)
        return reply.result()

    # -- the link ------------------------------------------------------

    async def run(self) -> None:
        """
        Hold the link, and dial again each time it goes (`Relink`). A
        server that was never reached, one that refused the link, and a
        failure that does not come back end it. A mirror keeps the last
        screen it was sent while no link is up.
        """
        self.running = True
        try:
            try:
                stream = await dial(self.address, None, **self.connect_with)
            except Exception as error:
                self.error = why(error)
                logger.warning("Could not reach the server at %s: %s", self.address, self.error)
                self.tree.pymux.show_message("Could not reach %s: %s" % (self.address, self.error))
                return
            try:
                await self.relink.hold(stream, self._hold)
            except Exception as error:
                # Lillecarl/pymux#879: a refused key is asked again only
                # when a person asks.
                self.error = why(error)
                logger.warning("The link to %s stops trying: %s", self.address, self.error)
                self.tree.pymux.show_message("%s: %s" % (self.address, self.error))
                self.say(tuple(link_ended_notice(self.server or self.address, self.error, self.address)))
        finally:
            self.running = False

    def say(self, lines: tuple[str, ...] | None) -> None:
        "Put a notice over each pane of this link, or take it away."
        for pane in self.tree.panes.values():
            pane.notice = lines
        self.tree.pymux.invalidate(Woke.TREE_MIRRORED)

    async def _hold(self, stream: anyio.abc.ByteStream) -> Exception | None:
        "Hold one link until it goes: why it went, or None when the owner refused it, so dialling again is no use."
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)
        self.accepted = False
        self.error = None
        self.restart = None
        self.connected = True
        self.stream = stream
        self._hello()
        refused: str | None = None
        lost: Exception = ConnectionResetError("the server closed the link")
        # This runs in the server's own scope: what it raises would end
        # every client, so the link's trouble stays the link's.
        try:
            async with stream, anyio.create_task_group() as tasks:
                tasks.start_soon(self._write, stream)
                reader = PacketReader()
                while refused is None:
                    try:
                        data = await stream.receive()
                    except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError) as error:
                        if str(error):
                            lost = ConnectionResetError(str(error))
                        break
                    for packet in reader.feed(data):
                        if self.accepted:
                            self._take(packet)
                            continue
                        refused = self._refusal(packet)
                        if refused is not None:
                            break
                if refused is not None:
                    self.error = refused
                    logger.warning("The link to %s cannot work: %s", self.address, refused)
                    self.tree.pymux.show_message("%s: %s" % (self.address, refused))
                tasks.cancel_scope.cancel()
        except Exception as error:
            lost = error
            logger.exception("The link to %s failed.", self.address)
        finally:
            self.connected = False
            self.stream = None
            self._send.close()
            self.tree.pymux.drag_and_drop.cut_off(
                [pane for pane in self.tree.panes.values() if not isinstance(pane.terminal, WindowWidget)]
            )
        if refused is not None:
            self.say(None)
            return None
        if self.restart is not None:
            # An owner that said it restarts is waited for until its window ends.
            lost = Restarting(*self.restart)
        logger.info("The link to %s went: %s", self.address, why(lost))
        return lost

    async def _write(self, stream: anyio.abc.ByteStream) -> None:
        # A send that fails ends the link the read loop is holding.
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            async for encoded in self._receive:
                await stream.send(encoded)
        await stream.aclose()

    def _refusal(self, packet: Message) -> str | None:
        """
        Read the owner's first packet: why the link cannot work, or
        `None` once the two speak a version in common. An owner too old
        to say a version sends something else first.
        """
        kind = packet.get(Field.CMD)
        if kind == Packet.WIRE:
            # The reader turned to the format this names; the answer is next.
            return None
        if kind == Packet.PEER_REFUSED:
            return str(packet.get(Field.ERROR) or "the server refused the link")
        if kind != Packet.PEER_ACCEPTED:
            return "upgrade pymux on %s: it names no version of the peer link" % (self.address,)
        upgrade = peer_upgrade(packet.get(Field.VERSION), packet.get(Field.OLDEST))
        if upgrade == "there":
            return "upgrade pymux on %s: it speaks the peer link up to version %s, and this server needs %s" % (
                self.address,
                packet.get(Field.VERSION),
                OLDEST_PEER_VERSION,
            )
        if upgrade == "here":
            return "upgrade pymux on this server: %s speaks the peer link from version %s, and this one up to %s" % (
                self.address,
                packet.get(Field.OLDEST),
                PEER_VERSION,
            )
        self.accepted = True
        return None

    def _take(self, packet: Message) -> None:
        if packet.get(Field.CMD) == Packet.PING:
            self.send({Field.CMD: Packet.PONG})
            return
        if packet.get(Field.CMD) == Packet.RESTARTING:
            self.restart = (
                str(packet.get(Field.MESSAGE) or "the server restarts"),
                anyio.current_time() + float(packet.get(Field.WAIT) or 0),
            )
            return
        if packet.get(Field.CMD) == Packet.TREE:
            if packet.get(Field.SERVER):
                self.server = str(packet[Field.SERVER])
            try:
                self.tree.update(
                    str(packet.get(Field.SERVER) or self.address),
                    str(packet.get(Field.SERVER_ID) or ""),
                    packet.get(Field.ROWS) or {},
                )
            except Exception:
                # A tree this side cannot build leaves the last one standing.
                logger.exception("The sessions of %s could not be mirrored.", self.address)
            return
        pane_id = packet.get(Field.PANE_ID)
        if packet.get(Field.CMD) == Packet.MIRROR_OSC:
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            if pane is not None:
                self.tree.pymux.outward.forward_osc(
                    pane,
                    str(packet.get(Field.CODE, "")),
                    str(packet.get(Field.DATA, "")),
                    acts=bool(packet.get(Field.ACTS, True)),
                )
            return
        if packet.get(Field.CMD) == Packet.MIRROR_CLIPBOARD_WRITE:
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            write = _clipboard_write_of(packet)
            # Served under this server's own `set-clipboard`, as a
            # pane's OSC 52 is.
            if pane is not None and write is not None and self.tree.pymux.options.set_clipboard is Clipboard.ON:
                self.tree.pymux.rich_clipboard.serve_write(pane, write)
            return
        if packet.get(Field.CMD) == Packet.MIRROR_DND:
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            if pane is not None:
                try:
                    dnd = DndPacket.decode(str(packet.get(Field.DATA, "")))
                except Malformed:
                    return
                self.tree.pymux.drag_and_drop.pane_said(pane, dnd)
            return
        if packet.get(Field.CMD) == Packet.MIRROR_KITTY_TRANSFER:
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            if pane is not None:
                try:
                    command = TransferCommand.decode(str(packet.get(Field.DATA, "")))
                except TransferMalformed:
                    return
                self.tree.pymux.kitty_transfers.pane_said(pane, command)
            return
        if packet.get(Field.CMD) == Packet.MIRROR_KITTY_TRANSFER_TAKEN:
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            if pane is not None:
                self.tree.pymux.kitty_transfers.owner_took(pane, str(packet.get(Field.DATA, "")))
            return
        if packet.get(Field.CMD) == Packet.MIRROR_CLIPBOARD_READ:
            self.tree.pymux.rich_clipboard.read_for_owner(self, str(packet.get(Field.DATA, "")))
            return
        if packet.get(Field.CMD) == Packet.MIRROR_COPIED:
            # A person's copy: the paste buffer of the sessions here, as a
            # copy mode here fills it (without the selection type), and
            # the terminal of the client a person here used last.
            pymux = self.tree.pymux
            text = str(packet.get(Field.DATA, ""))
            pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
            lender = None if pane is None else self._lender_used_last(pane)
            if pane is not None and lender is not None:
                # A person at a server further on reads copy mode through
                # this one, and the copy is theirs.
                lender.queue({Field.CMD: Packet.MIRROR_COPIED, Field.PANE_ID: int(pane.pane_id), Field.DATA: text})
                return
            if text:
                pymux.clipboard.set_data(ClipboardData(text))
            pymux.outward.write_user_clipboard(text, client=pymux.the_client_to_tell())
            return
        sink = self.sinks.get(pane_id) if isinstance(pane_id, int) else None
        if sink is None:
            return
        pane = self.tree.panes.get(pane_id) if isinstance(pane_id, int) else None
        if pane is not None and pane.notice is not None:
            # The link is back for this pane, and the page under the notice is the owner's again.
            pane.notice = None
        try:
            sink.take(packet)
        except Exception:
            # One mirror that cannot take what it was sent must not end
            # the link for the others.
            logger.exception("Mirror of pane %s on %s failed a packet.", pane_id, self.address)

    def _lender_used_last(self, pane: Pane) -> PeerLink | None:
        "The server that mirrors `pane` from here, when a person there used it after every client here."
        pymux = self.tree.pymux
        lenders = [link for link in pymux.peer_links() if any(source.pane is pane for source in link.sources.values())]
        lender = max(lenders, key=lambda link: link.last_used, default=None)
        latest = pymux.latest_client()
        if lender is None or (latest is not None and latest.last_used >= lender.last_used):
            return None
        return lender
