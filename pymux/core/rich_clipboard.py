"""
kitty's clipboard protocol (OSC 5522) between the panes and the
terminals of the clients. Lillecarl/pymux#893.

A pane's write holds any MIME type. It goes into the paste buffer of
the session with all of them, and to every client's terminal: as OSC
5522 to one that speaks it, and as OSC 52 with the text to one that
does not. `set-clipboard` gates it as it gates OSC 52. A peer that
mirrors the pane gets the whole write, and serves it the same way.

A pane's read goes to one terminal: the one whose paste event gave the
pane its password, or else the one a person on the pane's session used
last. That terminal asks the person; kitty does by default. One pane
read is in flight at a time, and another pane hears EBUSY, as the spec
asks of a multiplexer.

A pane that sets mode ?5522 gets paste events. The terminal of a client
whose focused pane set it has the mode set too, and then sends no
bracketed paste: an event that finds the focus moved on to a pane
without the mode is read as text by pymux and pasted.

A mirrored pane's read goes to a peer when a person there was last on
the pane, or when its password came from a paste event there. The
frames carry the pane's mode, so a peer's terminal sends paste events
too. Lillecarl/pymux#902.

Every packet pymux sends a terminal carries an id of pymux's own, so
that an answer is known for pymux's and never reaches a pane as keys.
"""

from __future__ import annotations

import itertools
import time
import weakref
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, ClassVar, Final

from libpymux.protocol import PEER_RICH_CLIPBOARD, Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.clipboard import ClipboardData
from ptterm import Terminal
from pyte.keep import Keep
from pyte.kitty_clipboard import ClipboardRead, ClipboardRequest, ClipboardWrite, answer_to
from pyte.modes import PrivateMode, flag_of
from pyte.osc import Osc
from pyte.screen import Screen
from pyte.sequences.kitty_clipboard import (
    LIST_TYPES,
    ClipboardPacket,
    PacketType,
    Status,
    b64,
    data_packets,
    strict_b64decode,
)

from ..connection.client_io import PASTE_EVENTS_OFF, PASTE_EVENTS_ON
from ..enums import Clipboard, Woke
from ..log import logger
from ..mirror.program import RemoteProgram
from ..notify.osc import build_osc

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..connection.server import ServerConnection
    from ..mirror.link import PeerLink
    from ..mirror.peer import Peer
    from .main import Pymux
    from .pane import Pane

__all__ = ["TEXT_TYPES", "RichClipboard", "RichClipboardData", "text_of", "wants_paste_events"]

#: How long, in seconds, a pane's read may hold the one place. kitty
#: asks the person, who may take a while; a read after this takes the
#: place, and the old one hears EBUSY.
READ_PATIENCE: Final = 120.0

#: The name a read under a paste event's password gives, as the spec asks.
PASTE_EVENT_NAME: Final = "Paste event"

#: How many paste event passwords are remembered, each with its terminal.
_PASSWORDS_KEPT: Final = 16

#: The statuses after which more packets of a read follow.
_GOING_ON: Final = frozenset([Status.OK, Status.DATA])

#: The MIME types that hold the text of a copy, best first. The X11
#: names are what kitty and Wayland offer beside "text/plain".
TEXT_TYPES: Final = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")

#: The start of every id pymux gives a packet to a terminal.
_OUR_IDS: Final = "pymux-"


class RichClipboardData(ClipboardData):
    """
    A paste buffer that holds every MIME type of a copy. `text` is its
    text, which `paste-buffer` pastes; `types` holds them all.

    Only the text lives through a server upgrade: a write may hold
    64 MB, too much for the store a snapshot writes.
    """

    def __init__(self, text: str, types: dict[str, bytes]) -> None:
        super().__init__(text)
        self.types = types


def wants_paste_events(pane: Pane | None) -> bool:
    "Whether the program in `pane` set kitty's paste events mode."
    screen = None if pane is None else pane.screen
    return isinstance(screen, Screen) and flag_of(PrivateMode.PASTE_EVENTS) in screen.mode


@dataclass
class _Reading:
    "A read pymux sent a terminal under its own id."

    connection: ServerConnection
    started: float
    #: The pane whose read this is, and its id; None for pymux's own
    #: read of a paste event's text.
    pane: Pane | None = None
    pane_id: str = ""
    #: pymux's own read: the type it asked for, the pane the text goes
    #: to, and the text so far.
    mime: str = ""
    paste_into: Pane | None = None
    parts: list[bytes] = field(default_factory=list)
    #: At the owner, the peer whose terminal answers the pane's read in
    #: place of `connection`'s.
    link: PeerLink | None = None
    #: At a peer, the owner whose pane asked, and the owner's id of the read.
    owner: Peer | None = None
    owner_id: str = ""

    def gone(self, pymux: Pymux) -> bool:
        "Whether the terminal that would answer is gone."
        if self.link is not None:
            return self.link.connection.closed
        return self.connection not in pymux._client_states


@dataclass
class _PasteEvent:
    "A paste event arriving from a terminal, for the pane focused when it began."

    pane: Pane | None
    relayed: bool
    primary: bool
    password: str
    types: tuple[str, ...] = ()


def _keep(passwords: dict[str, Any], password: str, holder: object) -> None:
    "Remember who gave `password`, forgetting the oldest past `_PASSWORDS_KEPT`."
    passwords[password] = holder
    while len(passwords) > _PASSWORDS_KEPT:
        del passwords[next(iter(passwords))]


def text_of(types: dict[str, bytes]) -> str | None:
    "The text among `types`, or None."
    for mime in TEXT_TYPES:
        if mime in types:
            return types[mime].decode("utf-8", "replace")
    return None


class RichClipboard:
    "The server's half of OSC 5522."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # A read in flight is lost with its connection: the client
        # reconnects, and the pane may ask again.
        "_ids": Keep.DROPPED,
        "_reads": Keep.DROPPED,
        "_events": Keep.DROPPED,
        "_passwords": Keep.DROPPED,
        "_peer_passwords": Keep.DROPPED,
        # A terminal left with the mode set sends paste events nobody
        # asked for; each becomes a text paste.
        "_events_on": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._ids = itertools.count(1)
        #: The reads sent to terminals, by pymux's id.
        self._reads: dict[str, _Reading] = {}
        #: The paste event each terminal is in the middle of.
        self._events: weakref.WeakKeyDictionary[ServerConnection, _PasteEvent] = weakref.WeakKeyDictionary()
        #: The terminal each recent paste event password came from.
        self._passwords: dict[str, ServerConnection] = {}
        #: The peer each recent paste event password of a mirrored pane came from.
        self._peer_passwords: dict[str, PeerLink] = {}
        #: The terminals told to send paste events.
        self._events_on: weakref.WeakSet[ServerConnection] = weakref.WeakSet()

    def _new_id(self) -> str:
        return _OUR_IDS + str(next(self._ids))

    def _send(self, connection: ServerConnection, packet: ClipboardPacket) -> None:
        connection.forward_osc(str(packet))
        self.pymux.invalidate(Woke.CLIPBOARD_ASKED)

    def asked(self, pane: Pane, request: ClipboardRequest) -> None:
        "A pane's finished write or its read. Never raises: this runs on the pane's read path."
        try:
            if isinstance(request, ClipboardWrite):
                self._write(pane, request)
            else:
                self._read(pane, request)
        except Exception:
            logger.exception("Serving a clipboard request of pane %s failed.", pane.pane_id)

    def _answer(self, pane: Pane, packet: ClipboardPacket) -> None:
        "Tell `pane`'s program. A pane whose program has gone hears nothing."
        screen = pane.terminal.screen
        if not isinstance(screen, Screen):
            return
        program = pane.process
        if isinstance(program, RemoteProgram):
            # A mirrored screen drops its replies, and a paste event
            # relayed to one is a person's input for the owner's program.
            program.write_input(screen.encode_report(packet))
            return
        try:
            screen.reply(packet)
        except OSError:
            logger.debug("Pane %s went before its clipboard answer.", pane.pane_id)

    def _write(self, pane: Pane, write: ClipboardWrite) -> None:
        if self.pymux.options.set_clipboard is not Clipboard.ON:
            self._answer(pane, answer_to(write, Status.EPERM))
            return
        self.serve_write(pane, write)
        self._answer(pane, answer_to(write, Status.DONE))

    def serve_write(self, pane: Pane, write: ClipboardWrite) -> None:
        """
        Give a write of `pane` to the paste buffer, the terminal of each
        client, and each peer that hears the pane. A peer serves it the
        same way, under its own `set-clipboard` (`PeerLink.pane_wrote`).
        """
        types = write.types()
        text = text_of(types)
        if not write.primary:
            self.pymux.clipboard.set_data(RichClipboardData(text or "", types))

        five = self._packets(write)
        fifty_two = None
        param = None
        if text:
            param = ("p" if write.primary else "c") + ";" + b64(text)
            fifty_two = build_osc(Osc.CLIPBOARD, param)
        for link in self.pymux.peer_links():
            link.pane_wrote(pane, write, param)
        for connection in self.pymux._client_states:
            # No connection is a socket command's temporary client.
            if connection is None:
                continue
            if connection.replies.kitty_clipboard:
                for packet in five:
                    connection.forward_osc(packet)
            elif fifty_two is not None:
                connection.forward_osc(fifty_two)
        self.pymux.invalidate(Woke.CLIPBOARD_WRITTEN)

    def _packets(self, write: ClipboardWrite) -> list[str]:
        "The write, spelled again for a terminal under an id of pymux's."
        head = ClipboardPacket(
            PacketType.WRITE,
            id=self._new_id(),
            primary=write.primary,
            password=write.password,
            name=write.name,
        )
        data = ClipboardPacket(PacketType.WDATA, id=head.id)
        packets = [str(head)]
        for mime, content in write.data.items():
            packets.extend(str(packet) for packet in data_packets(data, mime, content))
        by_target: dict[str, list[str]] = {}
        for alias, target in write.aliases.items():
            if target in write.data:
                by_target.setdefault(target, []).append(alias)
        for target, aliases in by_target.items():
            packets.append(
                str(ClipboardPacket(PacketType.WALIAS, mime=target, id=head.id, payload=b64(" ".join(aliases))))
            )
        packets.append(str(data))
        return packets

    def _read(self, pane: Pane, read: ClipboardRead) -> None:
        "Send a pane's read to one terminal, which asks the person."
        if self.pymux.options.set_clipboard is Clipboard.OFF:
            self._answer(pane, answer_to(read, Status.EPERM))
            return
        connection = self._passwords.get(read.password) if read.password else None
        link = self._peer_passwords.get(read.password) if read.password else None
        if link is not None:
            connection = link.connection
        if connection is None:
            client = self.pymux.outward.download_client(pane)
            link = self._peer_used_after(pane, client)
            connection = link.connection if link is not None else None if client is None else client.connection
        if link is None and (
            connection is None or connection not in self.pymux._client_states or not connection.replies.kitty_clipboard
        ):
            self._answer(pane, answer_to(read, Status.EPERM))
            return
        assert connection is not None
        now = time.monotonic()
        for our_id, reading in list(self._reads.items()):
            if now - reading.started > READ_PATIENCE or reading.gone(self.pymux):
                del self._reads[our_id]
                if reading.pane is not None:
                    self._answer(reading.pane, ClipboardPacket(PacketType.READ, Status.EBUSY, id=reading.pane_id))
        if any(reading.pane is not None for reading in self._reads.values()):
            self._answer(pane, answer_to(read, Status.EBUSY))
            return
        our_id = self._new_id()
        self._reads[our_id] = _Reading(connection, now, pane=pane, pane_id=read.id, link=link)
        packet = ClipboardPacket(
            PacketType.READ,
            id=our_id,
            primary=read.primary,
            password=read.password,
            name=read.name,
            payload=b64(" ".join(read.mimes)),
        )
        if link is not None:
            link.pane_reads(pane, str(packet))
        else:
            self._send(connection, packet)

    def _peer_used_after(self, pane: Pane, client: ClientState | None) -> PeerLink | None:
        """
        The peer whose person used a mirror of `pane` after `client`:
        its terminal answers the pane's read. Lillecarl/pymux#902.
        """
        links = [link for link in self.pymux.peer_links() if link.hears(pane) and link.version >= PEER_RICH_CLIPBOARD]
        link = max(links, key=lambda link: link.last_used, default=None)
        if link is None or (client is not None and client.last_used >= link.last_used):
            return None
        return link

    def peer_password(self, link: PeerLink, password: str) -> None:
        "A paste event at the peer gave a mirrored pane `password`: a read under it goes there."
        if password:
            _keep(self._peer_passwords, password, link)

    def peer_answered(self, link: PeerLink, sequence: str) -> None:
        "One packet of a peer terminal's answer to a pane's read (`_read`)."
        packet = ClipboardPacket.parse(sequence)
        reading = None if packet is None else self._reads.get(packet.id)
        if packet is None or reading is None or reading.link is not link:
            return
        self._read_answered(packet.id, reading, packet)

    def read_for_owner(self, owner: Peer, sequence: str) -> None:
        """
        The owner's read for a mirrored pane: the terminal of the client
        a person here used last answers it, and the answer goes back
        (`peer_answered`). Under this server's own `set-clipboard`.
        """
        packet = ClipboardPacket.parse(sequence)
        if packet is None:
            return
        client = self.pymux.latest_client()
        connection = self._passwords.get(packet.password) if packet.password else None
        if connection is None and client is not None:
            connection = client.connection
        if (
            self.pymux.options.set_clipboard is Clipboard.OFF
            or connection is None
            or connection not in self.pymux._client_states
            or not connection.replies.kitty_clipboard
        ):
            owner.send_clipboard_answer(str(ClipboardPacket(PacketType.READ, Status.EPERM, id=packet.id)))
            return
        our_id = self._new_id()
        self._reads[our_id] = _Reading(connection, time.monotonic(), owner=owner, owner_id=packet.id)
        self._send(connection, replace(packet, id=our_id))

    def terminal_said(self, connection: ServerConnection, param: str) -> None:
        "One OSC 5522 packet from a client's terminal, after its code."
        try:
            packet = ClipboardPacket.decode(param)
        except ValueError:
            logger.warning("Dropped an OSC 5522 packet that does not parse.")
            return
        if packet.type == PacketType.WRITE and packet.id.startswith(_OUR_IDS):
            if packet.status != Status.DONE:
                logger.warning("The terminal of client %s refused a copy: %s.", connection.client_id, packet.status)
            return
        if packet.type != PacketType.READ:
            logger.debug("Dropped an OSC 5522 packet nobody waits for: %s.", packet.type)
        elif packet.id:
            reading = self._reads.get(packet.id)
            if reading is not None:
                self._read_answered(packet.id, reading, packet)
        else:
            self._paste_event(connection, packet)

    def _read_answered(self, our_id: str, reading: _Reading, packet: ClipboardPacket) -> None:
        "One packet of a terminal's answer to a read pymux sent."
        if packet.status not in _GOING_ON:
            del self._reads[our_id]
        if reading.owner is not None:
            reading.owner.send_clipboard_answer(str(replace(packet, id=reading.owner_id)))
            return
        if reading.pane is not None:
            self._answer(reading.pane, replace(packet, id=reading.pane_id))
            return
        if packet.status == Status.DATA and packet.mime == reading.mime:
            try:
                reading.parts.append(strict_b64decode(packet.payload))
            except ValueError:
                logger.warning("Dropped a paste whose data is not base64.")
                reading.paste_into = None
        elif packet.status == Status.DONE and reading.paste_into is not None:
            text = b"".join(reading.parts).decode("utf-8", "replace")
            self._paste(reading.connection, reading.paste_into, text)

    def _paste_event(self, connection: ServerConnection, packet: ClipboardPacket) -> None:
        """
        One packet of a paste event: the terminal lists what its
        clipboard holds, unasked, under a one-time password.
        """
        client = connection.client_state
        if client is None:
            return
        if packet.status == Status.OK:
            pane = self.pymux.focus.pane_of(client)
            relayed = self._serves_paste_events(pane)
            self._events[connection] = _PasteEvent(pane, relayed, packet.primary, packet.password)
            if packet.password:
                _keep(self._passwords, packet.password, connection)
                program = None if pane is None else pane.process
                if relayed and isinstance(program, RemoteProgram):
                    # The owner sends the pane's read under this
                    # password here, whoever it saw last.
                    program.peer.send({Field.CMD: Packet.MIRROR_CLIPBOARD_PASSWORD, Field.DATA: packet.password})
        event = self._events.get(connection)
        if event is None:
            return
        if packet.status not in _GOING_ON:
            del self._events[connection]
        if event.relayed and event.pane is not None:
            self._answer(event.pane, packet)
            return
        if packet.status == Status.DATA and packet.mime == LIST_TYPES:
            try:
                event.types = packet.words()
            except ValueError:
                event.types = ()
        elif packet.status == Status.DONE:
            self._read_for_a_paste(connection, event)

    def _read_for_a_paste(self, connection: ServerConnection, event: _PasteEvent) -> None:
        "A paste event the pane in focus did not ask for: read the text, to paste it."
        mime = next((mime for mime in TEXT_TYPES if mime in event.types), None)
        if mime is None or event.pane is None:
            return
        our_id = self._new_id()
        self._reads[our_id] = _Reading(connection, time.monotonic(), mime=mime, paste_into=event.pane)
        self._send(
            connection,
            ClipboardPacket(
                PacketType.READ,
                id=our_id,
                primary=event.primary,
                password=event.password,
                name=PASTE_EVENT_NAME,
                payload=b64(mime),
            ),
        )

    def _paste(self, connection: ServerConnection, pane: Pane, text: str) -> None:
        "Paste `text` into `pane`, as the terminal's bracketed paste would have."
        client = connection.client_state
        terminal = pane.terminal
        if not text or client is None or not isinstance(terminal, Terminal):
            return
        with set_app(client.app):
            terminal.terminal_control.paste(text)

    def _serves_paste_events(self, pane: Pane | None) -> bool:
        """
        Whether `pane` gets paste events. Not under `set-clipboard off`:
        the pane could not read what an event lists, and the paste would
        be lost.
        """
        return self.pymux.options.set_clipboard is not Clipboard.OFF and wants_paste_events(pane)

    def sync_paste_events(self, client: ClientState) -> None:
        "Set the paste events mode on a client's terminal while its focused pane has it set."
        connection = client.connection
        if connection is None:
            return
        wanted = self._serves_paste_events(self.pymux.focus.pane_of(client))
        if wanted == (connection in self._events_on):
            return
        if wanted:
            self._events_on.add(connection)
        else:
            self._events_on.discard(connection)
        connection.write_output_raw(PASTE_EVENTS_ON if wanted else PASTE_EVENTS_OFF)
        connection.flush_output()
