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
from pyte.osc import color_base_data

from ..client.defaults import is_ssh_url
from ..client.reconnect import link_notice
from ..client.relink import COUNTDOWN_STEP, RESTART_RETRY
from ..commands import CommandException
from ..enums import Woke
from ..log import logger
from .copying import place_of
from .dial import dial, dial_beside
from .program import HOLD_FOR_FRESH, RemoteProgram
from .tree import MirrorTree
from .tunnel import Tunnels

if TYPE_CHECKING:
    from prompt_toolkit.data_structures import Size
    from pyte.keys.events import KeyProfile
    from pyte.mouse import MouseAction

    from ..connection.client_state import ClientState
    from ..core.main import Pymux
    from ..core.pane import Pane
    from ..core.session import Session
    from ..core.window_tree import Window
    from ..terminals import PeerTerminal
    from .copying import Place
    from .link import PeerLink
    from .sink import MirrorSink

__all__ = ["Peer", "Peers"]


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
        #: `[address, pane id, *place]` of each mirror a loaded snapshot
        #: was in copy mode on (`copying`).
        self.returning_copies: list[list[Any]] = []
        #: `[client id, address, session id, window id]` of each client a
        #: loaded snapshot had on a mirrored session (`places`).
        self.returning_places: list[list[Any]] = []
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
        for row in self.returning_copies:
            peer = self.by_address.get(row[0]) if len(row) == 5 else None
            place = place_of(row[2:])
            if peer is not None and type(row[1]) is int and place is not None:
                peer.copies[row[1]] = place
        self.returning_copies = []
        until = anyio.current_time() + PLACE_KEPT
        for row in self.returning_places:
            peer = self.by_address.get(row[1]) if len(row) == 4 else None
            if peer is not None and all(type(one) is int for one in row[2:]):
                peer.places[str(row[0])] = ReturningPlace(row[2], row[3], until)
        self.returning_places = []

    def places(self) -> list[list[Any]]:
        "Each client on a mirrored session, by its id, with the link and the owner's session and window."
        found = []
        for client_state in self.pymux._client_states.values():
            client_id = client_state.connection.client_id
            if client_state.temporary or not client_id:
                continue
            remote = client_state.session.remote
            if remote is None or remote.peer.address not in self.by_address:
                continue
            window = client_state.session.arrangement.get_active_window_for(client_state.app)
            window_id = remote.peer.tree.id_of_window(window) if window is not None else None
            if window_id is not None:
                found.append([client_id, remote.peer.address, remote.session_id, window_id])
        return found

    def return_clients(self) -> None:
        "Put each returning client back on its mirrored session, once both are here."
        for peer in self.by_address.values():
            peer.return_clients()

    def copying(self) -> list[tuple[str, int, Place]]:
        "Each mirror here in copy mode, by address and the owner's pane id, with where it stands."
        return [
            (address, pane_id, sink.copying)
            for address, peer in self.by_address.items()
            for pane_id, sink in sorted(peer.sinks.items())
            if sink.copying is not None
        ]

    def of(self, address: str, **connect_with: Any) -> Peer:
        "The link to `address`, opened in the server's scope if there is none yet."
        peer = self.by_address.get(address)
        if peer is None:
            if self.pymux.tasks is None:
                raise RuntimeError("A peer link was asked for outside `Pymux.running`, so nothing would hold it.")
            peer = self.by_address[address] = Peer(self.pymux, address, **connect_with)
            peer.tell_terminal(self.pymux.terminals.peer_terminal())
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


#: How long a peer waits to dial again after a link goes, at first and at most.
#: An owner's hot upgrade takes a second or two.
FIRST_REDIAL = 0.25
LAST_REDIAL = 5.0


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
        "paused": Keep.DROPPED,
        "watching": Keep.DROPPED,
        "copies": Keep.DROPPED,
        "connected": Keep.DROPPED,
        "error": Keep.DROPPED,
        "_send": Keep.DROPPED,
        "_receive": Keep.DROPPED,
        "_terminal_told": Keep.DROPPED,
        "accepted": Keep.DROPPED,
        "_sizes_told": Keep.DROPPED,
        "stream": Keep.DROPPED,
        "tree": Keep.DROPPED,
        "server": Keep.DROPPED,
        "restart": Keep.DROPPED,
        "places": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, address: str, **connect_with: Any) -> None:
        self.address = address
        self.connect_with = connect_with
        self.sinks: dict[int, MirrorSink] = {}
        #: The mirrors nobody here watches now.
        self.paused: set[int] = set()
        #: This side asked for the owner's sessions.
        self.watching = False
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
        self._terminal_told: PeerTerminal | None = None
        self._sizes_told: dict[int, Size] = {}
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
            client_state.connection.client_id: client_state
            for client_state in pymux._client_states.values()
            if client_state.connection.client_id and not client_state.temporary
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

    def _hello(self) -> None:
        "Start a new link from what this side holds."
        # Zstd only across a network, as a client offers it.
        offer = [MSGPACK_ZSTD, MSGPACK] if is_ssh_url(self.address) else [MSGPACK]
        self.send(
            {Field.CMD: Packet.PEER, Field.WIRE: offer, Field.VERSION: PEER_VERSION, Field.OLDEST: OLDEST_PEER_VERSION}
        )
        if self.watching:
            self.send({Field.CMD: Packet.WATCH})
        if self._terminal_told is not None:
            self._send_terminal(self._terminal_told)
        if self._sizes_told:
            self._send_sizes(self._sizes_told)
        for pane_id, sink in self.sinks.items():
            self.send({Field.CMD: Packet.MIRROR, Field.PANE_ID: pane_id})
            if pane_id in self.paused:
                self.pause(pane_id)
            if sink.copying is not None:
                self.copy_mode(pane_id, sink.copying)

    # -- what a mirror asks --------------------------------------------

    def mirror(self, pane_id: int, sink: MirrorSink) -> None:
        self.sinks[pane_id] = sink
        self.send({Field.CMD: Packet.MIRROR, Field.PANE_ID: pane_id})
        place = self.copies.pop(pane_id, None)
        if place is not None:
            sink.copying = place
            self.copy_mode(pane_id, place)

    def stop(self, pane_id: int) -> None:
        self.paused.discard(pane_id)
        if self.sinks.pop(pane_id, None) is not None:
            self.send({Field.CMD: Packet.MIRROR_STOP, Field.PANE_ID: pane_id})

    def pause(self, pane_id: int) -> None:
        self.paused.add(pane_id)
        self.send({Field.CMD: Packet.MIRROR_PAUSE, Field.PANE_ID: pane_id})

    def resume(self, pane_id: int) -> None:
        self.paused.discard(pane_id)
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

    def mouse(self, pane_id: int, action: MouseAction, column: int, row: int) -> None:
        self.send(
            {
                Field.CMD: Packet.MIRROR_MOUSE,
                Field.PANE_ID: pane_id,
                Field.MOUSE: {"action": int(action), "column": column, "row": row},
            }
        )

    def copy_mode(self, pane_id: int, at: Place | None = None) -> None:
        packet: Message = {Field.CMD: Packet.MIRROR_COPY, Field.PANE_ID: pane_id}
        if at is not None:
            packet[Field.COPYING] = list(at)
        self.send(packet)

    def tell_sizes(self, sizes: dict[int, Size]) -> None:
        "Tell the owner the room each of its windows has here, when that changed."
        if sizes == self._sizes_told:
            return
        self._sizes_told = sizes
        self._send_sizes(sizes)

    def _send_sizes(self, sizes: dict[int, Size]) -> None:
        self.send(
            {
                Field.CMD: Packet.PEER_SIZES,
                Field.SIZES: [[window_id, size.rows, size.columns] for window_id, size in sorted(sizes.items())],
            }
        )

    def watch(self) -> None:
        "Ask the owner for its sessions, now and on every change."
        self.watching = True
        self.send({Field.CMD: Packet.WATCH})

    def focus(self, pane_id: int, focused: bool) -> None:
        self.send({Field.CMD: Packet.MIRROR_FOCUS, Field.PANE_ID: pane_id, Field.FOCUSED: focused})

    def tell_terminal(self, terminal: PeerTerminal) -> None:
        "Tell the owner what the terminals here say, when it changed (`Terminals.tell_peers`)."
        if terminal == self._terminal_told:
            return
        self._terminal_told = terminal
        self._send_terminal(terminal)

    def _send_terminal(self, terminal: PeerTerminal) -> None:
        self.send(
            {
                Field.CMD: Packet.PEER_TERMINAL,
                Field.CELL_SIZE: None if terminal.cell_size is None else list(terminal.cell_size),
                Field.COLOR_BASE: None if terminal.color_base is None else color_base_data(terminal.color_base),
                Field.KEYBOARD: terminal.keyboard_source_flags,
                Field.THEME: terminal.theme,
            }
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
        Hold the link, and dial again each time it goes. A server that
        was never reached, or one that refused the link, ends it. A
        mirror keeps the last screen it was sent while no link is up.
        """
        linked = False
        wait = FIRST_REDIAL
        while True:
            if linked:
                self._say_gone(0.0, trying=True)
            try:
                stream = await dial(self.address, **self.connect_with)
            except Exception as error:
                self.error = str(error) or type(error).__name__
                if not linked:
                    logger.warning("Could not reach the server at %s: %s", self.address, self.error)
                    return
                wait = await self._count_down(wait)
                continue
            linked = True
            self.restart = None
            wait = FIRST_REDIAL
            if await self._hold(stream):
                self._say_gone(None)
                return
            wait = await self._count_down(wait)

    async def _count_down(self, wait: float) -> float:
        """
        Wait to dial again, with the panes saying so, and answer the
        wait after the next. An owner that restarts is dialled every
        `RESTART_RETRY` until its window ends: its next image answers
        within about a second.
        """
        restarting = self.restart is not None and anyio.current_time() < self.restart[1]
        if restarting:
            wait = RESTART_RETRY
        ends_at = anyio.current_time() + wait
        while (left := ends_at - anyio.current_time()) > 0:
            self._say_gone(left)
            await anyio.sleep(min(left, COUNTDOWN_STEP))
        return wait if restarting else min(wait * 2, LAST_REDIAL)

    def _say_gone(self, seconds: float | None, trying: bool = False) -> None:
        "Put the notice a client draws while its server is gone over each pane of this link, or take it away."
        lines = None
        if seconds is not None:
            why = self.restart[0] if self.restart is not None else (self.error or "the link went")
            lines = tuple(link_notice(self.server or self.address, why, seconds, trying))
        for pane in self.tree.panes.values():
            pane.notice = lines
        self.tree.pymux.invalidate(Woke.TREE_MIRRORED)

    async def _hold(self, stream: anyio.abc.ByteStream) -> bool:
        "Hold one link until it goes. True when the owner refused it, so dialling again is no use."
        self._send, self._receive = anyio.create_memory_object_stream[bytes](math.inf)
        self.accepted = False
        self.error = None
        self.connected = True
        self.stream = stream
        self._hello()
        refused: str | None = None
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
                        self.error = str(error) or "the server closed the link"
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
            self.error = str(error) or type(error).__name__
            logger.exception("The link to %s failed.", self.address)
        finally:
            self.connected = False
            self.stream = None
            self._send.close()
            logger.info("The link to %s went: %s", self.address, self.error)
        return refused is not None

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
                    pane, str(packet.get(Field.CODE, "")), str(packet.get(Field.DATA, ""))
                )
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
        lenders = [
            connection.peer
            for connection in pymux.connections
            if connection.peer is not None and any(source.pane is pane for source in connection.peer.sources.values())
        ]
        lender = max(lenders, key=lambda link: link.last_used, default=None)
        latest = pymux.latest_client()
        if lender is None or (latest is not None and latest.last_used >= lender.last_used):
            return None
        return lender
