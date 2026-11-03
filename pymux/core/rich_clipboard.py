"""
kitty's clipboard protocol (OSC 5522) between the panes and the
terminals of the clients. Lillecarl/pymux#893.

A pane's write holds any MIME type. It goes into the paste buffer of
the session with all of them, and to every client's terminal: as OSC
5522 to one that speaks it, and as OSC 52 with the text to one that
does not. `set-clipboard` gates it as it gates OSC 52.

Every packet pymux sends a terminal carries an id of pymux's own, so
that an answer is known for pymux's and never reaches a pane as keys.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, ClassVar, Final

from prompt_toolkit.clipboard import ClipboardData
from pyte.keep import Keep
from pyte.kitty_clipboard import ClipboardRead, ClipboardRequest, ClipboardWrite, answer_to
from pyte.osc import Osc
from pyte.sequences.kitty_clipboard import ClipboardPacket, PacketType, Status, b64, data_packets

from ..enums import Clipboard, Woke
from ..log import logger
from ..notify.osc import build_osc

if TYPE_CHECKING:
    from ..connection.server import ServerConnection
    from .main import Pymux
    from .pane import Pane

__all__ = ["TEXT_TYPES", "RichClipboard", "RichClipboardData", "text_of"]

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
        "_ids": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._ids = itertools.count(1)

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
        pane.terminal.screen.reply(packet)

    def _write(self, pane: Pane, write: ClipboardWrite) -> None:
        if self.pymux.options.set_clipboard is not Clipboard.ON:
            self._answer(pane, answer_to(write, Status.EPERM))
            return
        types = write.types()
        text = text_of(types)
        if not write.primary:
            self.pymux.clipboard.set_data(RichClipboardData(text or "", types))

        five = self._packets(write)
        fifty_two = None
        if text:
            param = ("p" if write.primary else "c") + ";" + b64(text)
            fifty_two = build_osc(Osc.CLIPBOARD, param)
            # A peer that watches this server serves the text under its
            # own options, as it does a pane's OSC 52.
            for link in self.pymux.peer_links():
                link.pane_said(pane, Osc.CLIPBOARD, param)
        for connection in self.pymux._client_states:
            # No connection is a socket command's temporary client.
            if connection is None:
                continue
            if connection.replies.kitty_clipboard:
                for packet in five:
                    connection.forward_osc(packet)
            elif fifty_two is not None:
                connection.forward_osc(fifty_two)
        self._answer(pane, answer_to(write, Status.DONE))
        self.pymux.invalidate(Woke.CLIPBOARD_WRITTEN)

    def _packets(self, write: ClipboardWrite) -> list[str]:
        "The write, spelled again for a terminal under an id of pymux's."
        head = ClipboardPacket(
            PacketType.WRITE,
            id=_OUR_IDS + str(next(self._ids)),
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
        self._answer(pane, answer_to(read, Status.EPERM))

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
        logger.debug("Dropped an OSC 5522 packet nobody waits for: %s.", packet.type)
