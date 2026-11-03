"""
kitty's drag and drop protocol (OSC 72) between the panes and the
terminals of the clients. https://sw.kovidgoyal.net/kitty/dnd-protocol/.
Lillecarl/pymux#892.

**Drops.** pymux registers as a drop target with every terminal that
answers the query, so that a drop anywhere reaches it. It maps the cell
of each move to the pane under it. A pane whose program registered gets
the move, the drop and the data, at its own cells and under its own
`i`. Its answers go back up without it. A pane whose program did not
register is pymux's to serve: pymux takes a file list or text and
pastes it, as kitty pastes into a window that did not register. Files
from another machine are copied here first (`drop_files.RemoteCopy`),
and their copies' paths pasted.

**Drags.** pymux offers drags while a pane's program does. kitty sends
the gesture to pymux, and pymux sends it to the pane under it if that
pane offers. Everything after goes between that pane and the terminal.

The terminal keeps one machine id, the one pymux sent last. pymux sends
a pane's id before that pane's drop or drag, since a pane may run on
another machine, over ssh.

**Mirrors.** The owner of a mirrored pane puts what its program
registered in the frames (`KittyDnd.registration`), so the peer registers
with its terminals for it. The peer serves a drop or drag over the pane
as over its own, and its packets for the program cross as `mirror-dnd`.
The owner gives them to the program and keeps the link that sent them,
which then gets the program's answers. Lillecarl/pymux#903.
"""

from __future__ import annotations

import hashlib
import hmac
import shlex
import weakref
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from prompt_toolkit.application.current import set_app
from ptterm import Terminal
from pyte.keep import Keep
from pyte.kitty_dnd import KittyDnd
from pyte.screen import Screen
from pyte.sequences.kitty_dnd import MACHINE_ID, START_OFFERING, STOP_OFFERING, DndPacket, DndType, Malformed

from ..log import logger
from ..mirror.program import RemoteProgram
from .drop_files import RemoteCopy, decoded, drops_directory, new_drop_root, paths_of

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..connection.server import ServerConnection
    from ..mirror.link import PeerLink
    from .main import Pymux
    from .pane import Pane

__all__ = ["OWN_TYPES", "DragAndDrop", "server_machine_id"]

_URI_LIST: Final = "text/uri-list"

#: The MIME types pymux takes for a pane that did not register, best
#: first: a file list pastes as paths, text as itself.
OWN_TYPES: Final = (_URI_LIST, "text/plain;charset=utf-8", "text/plain")

#: The key kitty hashes a machine id with, as the spec says.
_MACHINE_ID_KEY: Final = b"tty-dnd-protocol-machine-id"

#: The `o` that refuses a drop, and the one that copies.
_REFUSED: Final = 0
_COPY: Final = 1

#: The `x` of a drag event that says the drag is over.
_DRAG_FINISHED: Final = 4

#: What a terminal says on `t=E` when a drag started.
_STARTED: Final = "OK"


def server_machine_id() -> str:
    """
    This machine's id, as kitty's `machine_id` hashes it: "1:" and the
    HMAC of /etc/machine-id. "" where there is none to read; kitty takes
    that as the terminal's own machine.
    """
    try:
        with open("/etc/machine-id", "rb") as file:
            mid = file.read().rstrip()
    except OSError:
        return ""
    return "1:" + hmac.new(_MACHINE_ID_KEY, mid, hashlib.sha256).hexdigest()


def _registration(pane: Pane | None) -> KittyDnd | None:
    "What the program in `pane` registered, or None for a pane with no pyte screen."
    screen = None if pane is None else pane.screen
    return screen.kitty_dnd if isinstance(screen, Screen) else None


def _drop_finished(packet: DndPacket) -> bool:
    "A program's `t=r` that ends its drop, rather than asking for data."
    return packet.type == DndType.REQUEST and not (packet.cell_x or packet.cell_y or packet.pixel_y)


def _drag_over(packet: DndPacket) -> bool:
    "A terminal's packet that ends the drag a program began."
    finished = packet.type == DndType.DRAG_EVENT and packet.cell_x == _DRAG_FINISHED
    failed = packet.type == DndType.DRAG_ERROR and packet.payload.partition(":")[0] != _STARTED
    return finished or failed


@dataclass
class _Drop:
    "A drag over one client's terminal, from its entry to the drop's end."

    #: The pane under it, and whether that pane's program takes it.
    pane: Pane | None = None
    relayed: bool = False
    #: The MIME types the terminal offers, space separated, and the
    #: operations it allows.
    mimes: str = ""
    allowed: int = 0
    #: A move or drop whose MIME list is still arriving in chunks.
    pending: DndPacket | None = None
    pending_mimes: str = ""
    dropped: bool = False
    #: pymux's own fetch: the type asked for, its index, and the data
    #: so far; whether the terminal said its files are on another
    #: machine, and their copy.
    fetching: str = ""
    index: int = 0
    parts: list[str] = field(default_factory=list)
    remote: bool = False
    copy: RemoteCopy | None = None


@dataclass
class _Terminal:
    "What pymux knows of one client's terminal and the drags over it."

    asked: bool = False
    speaks: bool = False
    #: The MIME list pymux registered with, or None before it did.
    accepting: str | None = None
    offering: bool = False
    #: The machine id the terminal holds for drops.
    machine_id: str = ""
    #: The terminal's chain of chunks in progress.
    incoming: KittyDnd = field(default_factory=KittyDnd)
    #: A pane's chain going up, which nothing else may interrupt.
    chain_up: bool = False
    drop: _Drop | None = None
    #: The pane whose drag the terminal is carrying.
    drag: Pane | None = None


class DragAndDrop:
    "The server's half of OSC 72."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # A drag in flight is lost with its connection. The panes keep
        # what they registered, and the next terminal is asked again.
        "_terminals": Keep.DROPPED,
        "machine_id": Keep.REBUILT,
        "drops": Keep.REBUILT,
        "_peer_drops": Keep.DROPPED,
        "_peer_drags": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._terminals: weakref.WeakKeyDictionary[ServerConnection, _Terminal] = weakref.WeakKeyDictionary()
        #: This machine's id, and the one of a program that named none.
        self.machine_id = server_machine_id()
        #: Where the copies of drops from another machine go.
        self.drops: Path = drops_directory()
        #: The peer whose terminal carries a drop, or a drag, over a pane
        #: here it mirrors.
        self._peer_drops: dict[Pane, PeerLink] = {}
        self._peer_drags: dict[Pane, PeerLink] = {}

    def _terminal(self, connection: ServerConnection) -> _Terminal:
        state = self._terminals.get(connection)
        if state is None:
            state = self._terminals[connection] = _Terminal()
        return state

    # The terminal.

    def asked(self, connection: ServerConnection) -> None:
        self._terminal(connection).asked = True

    def is_asked(self, connection: ServerConnection) -> bool:
        "Whether OSC 72 from this terminal is cut out of its keys."
        state = self._terminals.get(connection)
        return state is not None and state.asked

    def speaks(self, connection: ServerConnection) -> bool:
        state = self._terminals.get(connection)
        return state is not None and state.speaks

    def _send(self, connection: ServerConnection, packet: DndPacket) -> None:
        "Write one packet to a terminal now: a drag waits for no frame."
        client = connection.client_state
        if client is None or client.temporary:
            return
        connection.write_output_raw(str(packet))
        connection.flush_output()

    def _tell(self, pane: Pane, packet: DndPacket, client_id: int) -> None:
        """
        Write one packet to a pane's program, under its `i`. A program that
        has gone hears nothing. A mirrored pane's goes to its owner, which
        knows the `i`.
        """
        program = pane.process
        if isinstance(program, RemoteProgram):
            program.dnd(replace(packet, client_id=0))
            return
        screen = pane.screen
        if not isinstance(screen, Screen):
            return
        try:
            screen.reply(replace(packet, client_id=client_id))
        except OSError:
            logger.debug("Pane %s went before its drag and drop packet.", pane.pane_id)

    def sync(self, client: ClientState) -> None:
        """
        Register with a client's terminal for what the panes take part in:
        drops always, with every type a pane named; drags while a pane
        offers them. Never inside a pane's chain going up.
        """
        connection = client.connection
        if connection is None or client.temporary:
            return
        state = self._terminals.get(connection)
        if state is None or not state.speaks or state.chain_up:
            return
        mimes = self._accepted_mimes()
        if mimes != state.accepting:
            state.accepting = mimes
            # A registration clears the machine id the terminal holds.
            self._send(connection, DndPacket(DndType.ACCEPT, payload=mimes))
            state.machine_id = ""
            self._send_machine_id(connection, state, self.machine_id)
        offering = self._any_pane_offers()
        if offering != state.offering:
            state.offering = offering
            x = START_OFFERING if offering else STOP_OFFERING
            self._send(connection, DndPacket(DndType.OFFER, cell_x=x, payload=self.machine_id if offering else ""))

    def _panes(self) -> list[Pane]:
        return [
            pane for session in self.pymux.sessions for window in session.arrangement.windows for pane in window.panes
        ]

    def _accepted_mimes(self) -> str:
        names = list(OWN_TYPES)
        for pane in self._panes():
            dnd = _registration(pane)
            if dnd is not None and dnd.accepting:
                names.extend(name for name in dnd.accepted_mimes.split() if name not in names)
        return " ".join(names)

    def _any_pane_offers(self) -> bool:
        return any((dnd := _registration(pane)) is not None and dnd.offering for pane in self._panes())

    def _send_machine_id(self, connection: ServerConnection, state: _Terminal, machine_id: str) -> None:
        if machine_id != state.machine_id:
            state.machine_id = machine_id
            self._send(connection, DndPacket(DndType.ACCEPT, cell_x=MACHINE_ID, payload=machine_id))

    def terminal_said(self, connection: ServerConnection, param: str) -> None:
        "One OSC 72 packet from a client's terminal, after its code. Never raises."
        try:
            packet = self._terminal(connection).incoming.take(param)
            if packet is not None:
                self._from_terminal(connection, packet)
        except Exception:
            logger.exception("Serving a drag and drop packet of client %s failed.", connection.client_id)

    def _from_terminal(self, connection: ServerConnection, packet: DndPacket) -> None:
        state = self._terminal(connection)
        match packet.type:
            case DndType.QUERY:
                state.speaks = True
                if connection.client_state is not None:
                    self.sync(connection.client_state)
            case DndType.MOVE | DndType.DROP:
                self._moved(connection, state, packet)
            case DndType.REQUEST | DndType.REQUEST_ERROR:
                self._data_arrived(connection, state, packet)
            case DndType.OFFER:
                self._gesture(connection, state, packet)
            case DndType.DRAG_EVENT | DndType.DRAG_ERROR | DndType.REMOTE_ITEM:
                self._drag_news(state, packet)

    # Drops.

    def _moved(self, connection: ServerConnection, state: _Terminal, packet: DndPacket) -> None:
        "A move or the drop. A MIME list in chunks is gathered first."
        drop = state.drop
        if packet.cell_x == -1 and packet.cell_y == -1:
            if drop is not None and drop.relayed and drop.pane is not None:
                self._tell_drop_pane(drop.pane, DndPacket(DndType.MOVE, cell_x=-1, cell_y=-1))
            state.drop = None
            return
        if drop is None:
            drop = state.drop = _Drop()
        if packet.more:
            drop.pending = packet
            drop.pending_mimes += packet.payload
            return
        if drop.pending is not None:
            packet = replace(drop.pending, more=0, payload=drop.pending_mimes + packet.payload)
            drop.pending, drop.pending_mimes = None, ""
        offer_changed = bool(packet.payload) and packet.payload != drop.mimes
        if packet.payload:
            drop.mimes = packet.payload
        drop.allowed = packet.operation

        client = connection.client_state
        found = None if client is None else self._pane_at(client, packet.cell_x, packet.cell_y)
        pane = None if found is None else found[0]
        if pane is not drop.pane:
            self._leave(connection, drop)
            drop.pane = pane
            dnd = _registration(pane)
            drop.relayed = dnd is not None and dnd.accepting
            if pane is not None and not drop.relayed:
                self._own_status(connection, drop)
            elif drop.relayed and dnd is not None:
                self._send_machine_id(connection, state, dnd.machine_id or self.machine_id)
        elif pane is not None and not drop.relayed and offer_changed:
            self._own_status(connection, drop)
        is_drop = packet.type == DndType.DROP
        if found is not None and drop.relayed:
            pane, x, y = found
            pixel_x, pixel_y = self._pixels_in(connection, packet, pane)
            self._tell_drop_pane(
                pane,
                replace(packet, cell_x=x, cell_y=y, pixel_x=pixel_x, pixel_y=pixel_y, payload=drop.mimes, more=0),
            )
        if is_drop:
            drop.dropped = True
            if pane is None:
                self._send(connection, DndPacket(DndType.REQUEST))
                state.drop = None
            elif not drop.relayed:
                self._fetch(connection, drop)

    def _leave(self, connection: ServerConnection, drop: _Drop) -> None:
        "The drag moved off a pane: that pane hears it left, and the terminal that nothing takes it yet."
        if drop.pane is None:
            return
        if drop.relayed:
            self._tell_drop_pane(drop.pane, DndPacket(DndType.MOVE, cell_x=-1, cell_y=-1))
        self._send(connection, DndPacket(DndType.MOVE, operation=_REFUSED))

    def _tell_drop_pane(self, pane: Pane, packet: DndPacket) -> None:
        dnd = _registration(pane)
        self._tell(pane, packet, 0 if dnd is None else dnd.drop_client_id)

    def _pane_at(self, client: ClientState, x: int, y: int) -> tuple[Pane, int, int] | None:
        "The pane a client shows at cell (x, y) of its terminal, and the cell in the pane."
        if client.display_popup:
            return None
        for pane, at in client.layout_manager.pane_write_positions.items():
            if at.xpos <= x < at.xpos + at.width and at.ypos <= y < at.ypos + at.height:
                if pane.clock_mode or pane.is_copying_for(client.app) or not isinstance(pane.screen, Screen):
                    return None
                return pane, x - at.xpos, y - at.ypos
        return None

    def _pixels_in(self, connection: ServerConnection, packet: DndPacket, pane: Pane) -> tuple[int, int]:
        "The pixel of a move within the pane, where the cell size is known."
        graphics = connection.graphics
        client = connection.client_state
        at = None if client is None else client.layout_manager.pane_write_positions.get(pane)
        if at is None or not graphics.cell_size_known:
            return packet.pixel_x, packet.pixel_y
        return packet.pixel_x - at.xpos * graphics.cell_width, packet.pixel_y - at.ypos * graphics.cell_height

    def _own_types(self, drop: _Drop) -> list[str]:
        offered = drop.mimes.split()
        return [mime for mime in OWN_TYPES if mime in offered]

    def _own_status(self, connection: ServerConnection, drop: _Drop) -> None:
        "Tell the terminal whether pymux takes this drag, for a pane that did not register."
        wanted = self._own_types(drop)
        operation = _COPY if wanted and drop.allowed & _COPY else _REFUSED
        self._send(connection, DndPacket(DndType.MOVE, operation=operation, payload=" ".join(wanted)))

    def _fetch(self, connection: ServerConnection, drop: _Drop) -> None:
        "pymux's own drop: ask for the best type it takes."
        wanted = self._own_types(drop)
        if not wanted:
            self._finish_own(connection, _REFUSED)
            return
        drop.fetching = wanted[0]
        drop.index = drop.mimes.split().index(drop.fetching) + 1
        self._send_machine_id(connection, self._terminal(connection), self.machine_id)
        self._send(connection, DndPacket(DndType.REQUEST, cell_x=drop.index))

    def _finish_own(self, connection: ServerConnection, operation: int) -> None:
        self._send(connection, DndPacket(DndType.REQUEST, operation=operation))
        self._terminal(connection).drop = None

    def _data_arrived(self, connection: ServerConnection, state: _Terminal, packet: DndPacket) -> None:
        "Data, or an error, the terminal sends for a request."
        drop = state.drop
        if drop is None or drop.pane is None:
            return
        if drop.relayed:
            self._tell_drop_pane(drop.pane, packet)
            return
        if drop.copy is not None:
            for request in drop.copy.took(packet):
                self._send(connection, request)
            self._copied(connection, drop)
            return
        if not drop.fetching:
            return
        if packet.type == DndType.REQUEST_ERROR:
            logger.info("The terminal of client %s gave no drop data: %s.", connection.client_id, packet.payload)
            self._finish_own(connection, _REFUSED)
            return
        # On a file list, X=1 says its files are on another machine.
        drop.remote = drop.remote or packet.pixel_x == 1
        drop.parts.append(packet.payload)
        if packet.more or packet.payload:
            return
        try:
            text = decoded(drop.parts).decode("utf-8", "replace")
        except ValueError:
            logger.warning("Dropped a drop whose data is not base64.")
            self._finish_own(connection, _REFUSED)
            return
        if drop.fetching == _URI_LIST and drop.remote:
            self._copy(connection, drop, text)
            return
        pane, fetching = drop.pane, drop.fetching
        self._finish_own(connection, _COPY)
        if fetching == _URI_LIST:
            text = " ".join(shlex.quote(path) for path in paths_of(text))
        self._paste(connection, pane, text)

    def _copy(self, connection: ServerConnection, drop: _Drop, uri_list: str) -> None:
        "Files from another machine: pull each into a directory here, and paste their paths there."
        drop.copy = RemoteCopy(new_drop_root(self.drops), drop.index, uri_list)
        try:
            requests = drop.copy.start()
        except OSError as error:
            logger.warning("No place for a drop from another machine: %s.", error)
            self._finish_own(connection, _REFUSED)
            return
        for request in requests:
            self._send(connection, request)
        self._copied(connection, drop)

    def _copied(self, connection: ServerConnection, drop: _Drop) -> None:
        "Paste a copy that ended, or end the drop if it failed."
        copy = drop.copy
        if copy is None or not copy.done:
            return
        pasted = copy.pasted
        self._finish_own(connection, _COPY if pasted else _REFUSED)
        if pasted and drop.pane is not None:
            self._paste(connection, drop.pane, " ".join(shlex.quote(path) for path in pasted))

    def _paste(self, connection: ServerConnection, pane: Pane, text: str) -> None:
        client = connection.client_state
        terminal = pane.terminal
        if not text or client is None or not isinstance(terminal, Terminal):
            return
        with set_app(client.app):
            terminal.terminal_control.paste(text)

    # Drags.

    def _gesture(self, connection: ServerConnection, state: _Terminal, packet: DndPacket) -> None:
        "The person began a drag at a cell: it goes to the pane there, if that pane offers drags."
        client = connection.client_state
        found = None if client is None else self._pane_at(client, packet.cell_x, packet.cell_y)
        if found is None:
            return
        pane, x, y = found
        dnd = _registration(pane)
        if dnd is None or not dnd.offering:
            return
        state.drag = pane
        self._send(
            connection, DndPacket(DndType.OFFER, cell_x=START_OFFERING, payload=dnd.offer_machine_id or self.machine_id)
        )
        pixel_x, pixel_y = self._pixels_in(connection, packet, pane)
        self._tell(pane, replace(packet, cell_x=x, cell_y=y, pixel_x=pixel_x, pixel_y=pixel_y), dnd.drag_client_id)

    def _drag_news(self, state: _Terminal, packet: DndPacket) -> None:
        "What the terminal says of a drag a pane began: it goes to that pane."
        pane = state.drag
        dnd = _registration(pane)
        if pane is None or dnd is None:
            return
        self._tell(pane, packet, dnd.drag_client_id)
        if _drag_over(packet):
            state.drag = None

    # A peer's terminal, over a pane here.

    def peer_said(self, link: PeerLink, pane: Pane, param: str) -> None:
        "One OSC 72 packet from a peer's terminal for a pane here that it mirrors, after its code. Never raises."
        try:
            self._from_peer(link, pane, DndPacket.decode(param))
        except Malformed:
            pass
        except Exception:
            logger.exception("Serving a drag and drop packet of a peer for pane %s failed.", pane.pane_id)

    def _from_peer(self, link: PeerLink, pane: Pane, packet: DndPacket) -> None:
        dnd = _registration(pane)
        if dnd is None:
            return
        match packet.type:
            case DndType.MOVE if packet.cell_x == -1 and packet.cell_y == -1:
                self._peer_drops.pop(pane, None)
                self._tell(pane, packet, dnd.drop_client_id)
            case DndType.MOVE | DndType.DROP | DndType.REQUEST | DndType.REQUEST_ERROR:
                self._peer_drops[pane] = link
                self._tell(pane, packet, dnd.drop_client_id)
            case DndType.OFFER | DndType.DRAG_EVENT | DndType.DRAG_ERROR | DndType.REMOTE_ITEM:
                self._peer_drags[pane] = link
                self._tell(pane, packet, dnd.drag_client_id)
                if _drag_over(packet):
                    self._peer_drags.pop(pane, None)

    def link_gone(self, link: PeerLink) -> None:
        "A peer's link ended: what its terminals carried ends with it."
        for holders in (self._peer_drops, self._peer_drags):
            for pane in [pane for pane, holder in holders.items() if holder is link]:
                del holders[pane]

    # The panes.

    def pane_said(self, pane: Pane, packet: DndPacket) -> None:
        "One OSC 72 packet of a pane's program, whole. Never raises: this runs on the pane's read path."
        try:
            self._from_pane(pane, packet)
        except Exception:
            logger.exception("Serving a drag and drop packet of pane %s failed.", pane.pane_id)

    def _from_pane(self, pane: Pane, packet: DndPacket) -> None:
        dnd = _registration(pane)
        if dnd is None:
            return
        match packet.type:
            case DndType.QUERY:
                # Answered whatever terminal is attached: a program asks
                # once, at its start, and the clients change.
                self._tell(pane, DndPacket(DndType.QUERY), packet.client_id)
            case DndType.ACCEPT | DndType.STOP_ACCEPTING:
                self.sync_all()
            case DndType.OFFER if packet.cell_x in (START_OFFERING, STOP_OFFERING):
                self.sync_all()
            case DndType.MOVE | DndType.REQUEST:
                self._up_from_drop_pane(pane, packet)
            case _:
                self._up_from_drag_pane(pane, packet)

    def sync_all(self) -> None:
        "Register with every client's terminal for what the panes take part in now."
        for client in self.pymux._client_states.values():
            self.sync(client)

    def _up(self, connection: ServerConnection, state: _Terminal, packet: DndPacket) -> None:
        "A pane's packet, without its `i`, to the terminal. A chain stays whole."
        state.chain_up = bool(packet.more)
        self._send(connection, replace(packet, client_id=0))
        if not state.chain_up and connection.client_state is not None:
            self.sync(connection.client_state)

    def _up_from_drop_pane(self, pane: Pane, packet: DndPacket) -> None:
        for connection, state in list(self._terminals.items()):
            drop = state.drop
            if drop is not None and drop.relayed and drop.pane is pane:
                self._up(connection, state, packet)
                if _drop_finished(packet):
                    state.drop = None
                return
        link = self._peer_drops.get(pane)
        if link is not None:
            link.pane_dnd(pane, packet)
            if _drop_finished(packet):
                del self._peer_drops[pane]
            return
        if packet.type == DndType.REQUEST:
            dnd = _registration(pane)
            self._tell(
                pane,
                DndPacket(
                    DndType.REQUEST_ERROR,
                    cell_x=packet.cell_x,
                    cell_y=packet.cell_y,
                    pixel_y=packet.pixel_y,
                    payload="EPERM:no drop over this pane",
                ),
                0 if dnd is None else dnd.drop_client_id,
            )

    def _up_from_drag_pane(self, pane: Pane, packet: DndPacket) -> None:
        for connection, state in list(self._terminals.items()):
            if state.drag is pane:
                self._up(connection, state, packet)
                if packet.type == DndType.DRAG_ERROR and packet.cell_y == -1:
                    state.drag = None
                return
        link = self._peer_drags.get(pane)
        if link is not None:
            link.pane_dnd(pane, packet)
            if packet.type == DndType.DRAG_ERROR and packet.cell_y == -1:
                del self._peer_drags[pane]
            return
        refused = (packet.type == DndType.OFFER and not packet.cell_x) or (
            packet.type == DndType.DRAG_IMAGE and packet.cell_x < 0
        )
        if refused:
            dnd = _registration(pane)
            self._tell(pane, DndPacket(DndType.DRAG_ERROR, payload="EPERM"), 0 if dnd is None else dnd.drag_client_id)

    def forget(self, pane: Pane) -> None:
        "A pane went: a drop or drag of it ends, and the registrations shrink. Never raises: a pane's end runs on."
        try:
            self._forget(pane)
        except Exception:
            logger.exception("Ending the drag and drop of pane %s failed.", pane.pane_id)

    def _forget(self, pane: Pane) -> None:
        for connection, state in list(self._terminals.items()):
            if state.drop is not None and state.drop.pane is pane:
                if state.drop.copy is not None:
                    state.drop.copy.stop()
                if state.drop.dropped:
                    self._send(connection, DndPacket(DndType.REQUEST))
                else:
                    self._send(connection, DndPacket(DndType.MOVE, operation=_REFUSED))
                state.drop = None
            if state.drag is pane:
                self._send(connection, DndPacket(DndType.DRAG_ERROR, cell_y=-1))
                state.drag = None
        # The peer's own mirror of the pane goes too, and ends what it carried.
        self._peer_drops.pop(pane, None)
        self._peer_drags.pop(pane, None)
        self.sync_all()
