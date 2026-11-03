"""
kitty's file transfer protocol (OSC 5113) between the panes and the
terminals of the clients. https://sw.kovidgoyal.net/kitty/file-transfer-protocol/.
Lillecarl/pymux#912.

**A session goes to one terminal.** A program starts one with `send` or
`receive`. pymux gives it to the client a download would go to
(`Outward.download_client`), when that client's terminal transfers
files: kitty since 0.49.2, or any terminal under `file-transfer
passthrough`. kitty asks the person, and its answers carry the session's
id, which takes them back to the pane that started it. pymux keeps the
id as it is: a bypass password is hashed with it.

A session that no terminal takes is refused, as kitty refuses one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

from pyte.keep import Keep
from pyte.osc import Osc
from pyte.screen import Screen
from pyte.sequences.file_transfer import Action, Malformed, TransferCommand
from pyte.sequences.strings import OscString, StringReport

from ..enums import FileTransfer
from ..log import logger

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..connection.server import ServerConnection
    from .main import Pymux
    from .pane import Pane

__all__ = ["KittyTransfers"]

#: How long a session may sit idle before it is forgotten: kitty's
#: `EXPIRE_TIME`, ten minutes.
IDLE_SECONDS: Final = 600.0

#: The `quiet` that keeps even errors from the program.
_SILENT: Final = 2

_REFUSED: Final = "EPERM:pymux has no terminal here that takes a file transfer"
_TERMINAL_GONE: Final = "EIO:the terminal of the transfer went away"
_CANCELED: Final = "CANCELED"


@dataclass
class _Session:
    pane: Pane
    connection: ServerConnection
    #: When the program or the terminal last said something of it.
    used: float


class KittyTransfers:
    "The server's half of OSC 5113."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # A transfer in flight is lost with its connection.
        "_sessions": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._sessions: dict[tuple[ServerConnection, str], _Session] = {}

    def listens(self, connection: ServerConnection) -> bool:
        "Whether OSC 5113 from this terminal is cut out of its keys: it carries a session."
        return any(session.connection is connection for session in self._sessions.values())

    def forwards(self, client: ClientState) -> bool:
        "Whether a session goes to this client's terminal."
        connection = client.connection
        if connection is None or client.temporary:
            return False
        match client.options.file_transfer:
            case FileTransfer.PASSTHROUGH:
                return True
            case FileTransfer.PYMUX:
                return False
            case _:
                return connection.replies.identity.transfers_files()

    # The panes.

    def pane_said(self, pane: Pane, command: TransferCommand) -> None:
        "One OSC 5113 command of a pane's program. Never raises: this runs on the pane's read path."
        try:
            self._from_pane(pane, command)
        except Exception:
            logger.exception("Serving a file transfer command of pane %s failed.", pane.pane_id)

    def _from_pane(self, pane: Pane, command: TransferCommand) -> None:
        now = time.monotonic()
        self._prune(now)
        if command.action in (Action.SEND, Action.RECEIVE):
            client = self.pymux.outward.download_client(pane)
            if client is None or client.connection is None or not self.forwards(client):
                if command.quiet < _SILENT:
                    self._tell(pane, TransferCommand(Action.STATUS, id=command.id, status=_REFUSED))
                return
            self._sessions[(client.connection, command.id)] = _Session(pane, client.connection, now)
            self._send(client.connection, command)
            return
        session = next(
            (
                session
                for (connection, session_id), session in self._sessions.items()
                if session_id == command.id and session.pane is pane
            ),
            None,
        )
        if session is None:
            return
        session.used = now
        self._send(session.connection, command)

    def forget(self, pane: Pane) -> None:
        "A pane's program ended: its sessions end, and each terminal is told to drop its own."
        for key, session in list(self._sessions.items()):
            if session.pane is pane:
                del self._sessions[key]
                self._send(session.connection, TransferCommand(Action.CANCEL, id=key[1]))

    # The terminals.

    def terminal_said(self, connection: ServerConnection, param: str) -> None:
        "One OSC 5113 command from a client's terminal, after its code. Never raises."
        try:
            command = TransferCommand.decode(param)
        except Malformed:
            return
        try:
            key = (connection, command.id)
            session = self._sessions.get(key)
            if session is None:
                return
            session.used = time.monotonic()
            if command.action is Action.STATUS and not command.file_id and command.status == _CANCELED:
                del self._sessions[key]
            # As the terminal wrote it, and not written again.
            self._tell(session.pane, OscString(Osc.FILE_TRANSFER, param))
        except Exception:
            logger.exception("Serving a file transfer command of client %s failed.", connection.client_id)

    def client_gone(self, connection: ServerConnection) -> None:
        "A client went: each program whose session its terminal carried hears so."
        for key, session in list(self._sessions.items()):
            if session.connection is connection:
                del self._sessions[key]
                self._tell(session.pane, TransferCommand(Action.STATUS, id=key[1], status=_TERMINAL_GONE))

    def _prune(self, now: float) -> None:
        for key, session in list(self._sessions.items()):
            if now - session.used > IDLE_SECONDS:
                del self._sessions[key]

    def _send(self, connection: ServerConnection, command: TransferCommand) -> None:
        "Write one command to a terminal now: a transfer waits for no frame."
        connection.write_output_raw(str(command))
        connection.flush_output()

    def _tell(self, pane: Pane, answer: StringReport) -> None:
        "Write one command to a pane's program. A program that has gone hears nothing."
        screen = pane.screen
        if not isinstance(screen, Screen):
            return
        try:
            screen.reply(answer)
        except OSError:
            logger.debug("Pane %s went before its file transfer command.", pane.pane_id)
