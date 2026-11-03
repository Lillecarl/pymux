"""
kitty's file transfer protocol (OSC 5113) between the panes and the
terminals of the clients. https://sw.kovidgoyal.net/kitty/file-transfer-protocol/.
Lillecarl/pymux#912.

**A session goes to one client.** A program starts one with `send` or
`receive`. pymux gives it to the client a download would go to
(`Outward.download_client`). When that client's terminal transfers
files (kitty since 0.49.2, or any terminal under `file-transfer
passthrough`), the commands go to the terminal, and kitty asks the
person. Otherwise the pymux client is the terminal
(`pymux.client.kitty_transfer`), and asks the person itself. Either
way the answers carry the session's id, which takes them back to the
pane that started it. pymux keeps the id as it is: a bypass password is
hashed with it.

A session that neither takes is refused, as kitty refuses one.

**A mirrored pane's session can go to a peer** (`Route.PEER`): the
server whose person used a mirror of the pane after every client here
(`Outward.peer_used_after`). The peer serves it as a pane of its own,
and its answers come back over the link. So do its questions of pace:
the peer asks, and this side answers once the program took what came.

**A bypass password opens a session only through the pymux client**, or a
peer's. A pane encrypts it to this server's key (`kitty_bypass`), and
the client is sent its `sha256:` form. A real kitty holds its own key,
so a session that goes to it goes without the bypass, and kitty asks.

**A file the terminal sends waits in the terminal.** Past `HOLD_BYTES`
that the pane's program has not read, the server tells the client to
stop reading its terminal (`Packet.HOLD_INPUT`), and kitty, whose child
the client is, stops writing. The person's keys wait with it, so a
program that reads nothing for `STALL_SECONDS` loses its session and
the keys go again. A mirrored pane's backlog is on its owner, so a peer
counts what it passed on and asks the owner, as it asks "taken".
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

import anyio
from libpymux.protocol import PEER_FILE_TRANSFER, Field, Packet
from ptyhost.process import Process
from pyte.keep import Keep
from pyte.osc import Osc
from pyte.screen import Screen
from pyte.sequences.file_transfer import Action, Malformed, TransferCommand
from pyte.sequences.strings import OscString, StringReport

from ..enums import FileTransfer
from ..log import logger
from ..mirror.program import RemoteProgram
from ..pipes.base import BrokenPipeError
from .kitty_bypass import SHA256, BypassKey

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..connection.server import ServerConnection
    from ..mirror.link import PeerLink
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
_STALLED: Final = "EIO:the program read nothing of the transfer for %d seconds"

#: Bytes a pane may hold for its program before its client's terminal waits.
HOLD_BYTES: Final = 1024 * 1024

#: How long a held terminal waits on a program that reads nothing.
STALL_SECONDS: Final = 10.0


class Route(Enum):
    "Who answers a session as the terminal."

    TERMINAL = auto()
    PYMUX_CLIENT = auto()
    PEER = auto()


@dataclass
class _Session:
    pane: Pane
    connection: ServerConnection
    route: Route
    #: When the program or the terminal last said something of it.
    used: float
    #: For a mirrored pane: what the terminal sent since the owner last said its program took all.
    unanswered: int = 0


class KittyTransfers:
    "The server's half of OSC 5113."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        # A transfer in flight is lost with its connection.
        "_sessions": Keep.DROPPED,
        "_held": Keep.DROPPED,
        "_owner_takes": Keep.DROPPED,
        # Read again from its file beside the socket.
        "_key": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self._sessions: dict[tuple[ServerConnection, str], _Session] = {}
        #: The connections told to stop reading their terminal.
        self._held: set[ServerConnection] = set()
        #: What waits, on a peer, for the owner to say a mirrored pane's program took a part.
        self._owner_takes: dict[tuple[Pane, str], anyio.Event] = {}
        self._key: BypassKey | None = None

    @property
    def key(self) -> BypassKey:
        "The key every pane encrypts a bypass password to, as `KITTY_PUBLIC_KEY`."
        if self._key is None:
            socket_name = self.pymux.socket_name
            self._key = BypassKey(Path(socket_name + ".kitty-key") if socket_name else None)
        return self._key

    def listens(self, connection: ServerConnection) -> bool:
        "Whether OSC 5113 from this terminal is cut out of its keys: it carries a session."
        return any(
            session.connection is connection and session.route is Route.TERMINAL for session in self._sessions.values()
        )

    def route_for(self, client: ClientState) -> Route | None:
        "Who answers a session that goes to this client, or `None` for nobody."
        if client.temporary:
            return None
        chosen = client.options.file_transfer
        if chosen is FileTransfer.PASSTHROUGH or (
            chosen is FileTransfer.AUTO and client.client.replies.identity.transfers_files()
        ):
            return Route.TERMINAL
        return Route.PYMUX_CLIENT if client.client.answers_transfers else None

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
            link = self.pymux.outward.peer_used_after(pane, client, PEER_FILE_TRANSFER)
            if link is not None:
                session = _Session(pane, link.connection, Route.PEER, now)
                self._sessions[(link.connection, command.id)] = session
                self._send(session, self._bypass_for(session, command))
                return
            route = None if client is None else self.route_for(client)
            if client is None or route is None:
                if command.quiet < _SILENT:
                    self._tell(pane, TransferCommand(Action.STATUS, id=command.id, status=_REFUSED))
                return
            session = _Session(pane, client.client.connection, route, now)
            command = self._bypass_for(session, command)
            self._sessions[(client.client.connection, command.id)] = session
            self._send(session, command)
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
        self._send(session, command)

    def _bypass_for(self, session: _Session, command: TransferCommand) -> TransferCommand:
        """
        The command as whoever answers the session reads its bypass: none
        for a real kitty, and the `sha256:` form for the pymux client or a
        peer. One that does not open becomes a form that matches no
        password, so the session is refused, as kitty refuses it.
        """
        if not command.bypass:
            return command
        if session.route is Route.TERMINAL:
            return replace(command, bypass="")
        return replace(command, bypass=self.key.plain(command.id, command.bypass) or SHA256)

    def forget(self, pane: Pane) -> None:
        "A pane's program ended: its sessions end, and each terminal is told to drop its own."
        for key in [key for key in self._owner_takes if key[0] is pane]:
            self._owner_takes.pop(key).set()
        for key, session in list(self._sessions.items()):
            if session.pane is pane:
                del self._sessions[key]
                self._send(session, TransferCommand(Action.CANCEL, id=key[1]))

    # The terminals.

    def terminal_said(self, connection: ServerConnection, param: str) -> None:
        "One OSC 5113 command from a client's terminal or from the pymux client, after its code. Never raises."
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
            if session.route is Route.TERMINAL:
                if isinstance(session.pane.process, RemoteProgram):
                    session.unanswered += len(param)
                    self._pace_mirror(key, session)
                else:
                    self._pace(key, session)
        except Exception:
            logger.exception("Serving a file transfer command of client %s failed.", connection.name)

    async def taken(self, connection: ServerConnection, session_id: str) -> None:
        "Answer the pymux client once the session's program holds every part it sent."
        session = self._sessions.get((connection, session_id))
        if session is not None:
            await self._program_took(session.pane, session_id)
        with suppress(BrokenPipeError):
            connection.send_packet({Field.CMD: Packet.KITTY_TRANSFER_TAKEN, Field.DATA: session_id})

    async def taken_for_peer(self, link: PeerLink, pane: Pane, session_id: str) -> None:
        "Answer a peer once the program of `pane`, which it mirrors, holds every part the peer sent."
        await self._program_took(pane, session_id)
        link.pane_took(pane, session_id)

    def owner_took(self, pane: Pane, session_id: str) -> None:
        "The owner of a pane mirrored here says its program took what came (`taken`)."
        took = self._owner_takes.pop((pane, session_id), None)
        if took is not None:
            took.set()

    async def _program_took(self, pane: Pane, session_id: str) -> None:
        process = pane.process
        if isinstance(process, Process):
            await process.input_written()
        elif isinstance(process, RemoteProgram):
            took = self._owner_takes.setdefault((pane, session_id), anyio.Event())
            process.kitty_transfer_taken(session_id)
            await took.wait()

    def _pace(self, key: tuple[ServerConnection, str], session: _Session) -> None:
        process = session.pane.process
        connection = session.connection
        if connection in self._held or not isinstance(process, Process) or process.input_unwritten <= HOLD_BYTES:
            return
        self._held.add(connection)
        with suppress(BrokenPipeError):
            connection.send_packet({Field.CMD: Packet.HOLD_INPUT})
        connection.spawn(self._release_when_read(key, session, process))

    async def _release_when_read(self, key: tuple[ServerConnection, str], session: _Session, process: Process) -> None:
        "Let the terminal go on once the program read what waits, or once it stalls."
        connection = session.connection
        try:
            while True:
                before = process.input_unwritten
                with anyio.move_on_after(STALL_SECONDS) as waited:
                    await process.input_written()
                if not waited.cancelled_caught:
                    return
                if process.input_unwritten < before:
                    continue
                self._stalled(key, session)
                return
        finally:
            self._release(connection)

    def _pace_mirror(self, key: tuple[ServerConnection, str], session: _Session) -> None:
        "`_pace` for a pane mirrored here: the backlog is the owner's, so the owner says when it is read."
        connection = session.connection
        if connection in self._held or session.unanswered <= HOLD_BYTES:
            return
        self._held.add(connection)
        with suppress(BrokenPipeError):
            connection.send_packet({Field.CMD: Packet.HOLD_INPUT})
        connection.spawn(self._release_when_owner_took(key, session))

    async def _release_when_owner_took(self, key: tuple[ServerConnection, str], session: _Session) -> None:
        """
        Let the terminal go on once the owner says its program took all, or
        stall after `STALL_SECONDS`. The owner says nothing of a program
        that reads slowly, so a slow one stalls as one that reads nothing.
        """
        asked = session.unanswered
        try:
            with anyio.move_on_after(STALL_SECONDS) as waited:
                await self._program_took(session.pane, key[1])
            if waited.cancelled_caught:
                self._owner_takes.pop((session.pane, key[1]), None)
                self._stalled(key, session)
            else:
                session.unanswered -= asked
        finally:
            self._release(session.connection)

    def _stalled(self, key: tuple[ServerConnection, str], session: _Session) -> None:
        if self._sessions.pop(key, None) is not None:
            self._send(session, TransferCommand(Action.CANCEL, id=key[1]))
            self._tell(session.pane, TransferCommand(Action.STATUS, id=key[1], status=_STALLED % STALL_SECONDS))

    def _release(self, connection: ServerConnection) -> None:
        self._held.discard(connection)
        with suppress(BrokenPipeError):
            connection.send_packet({Field.CMD: Packet.RELEASE_INPUT})

    def client_gone(self, connection: ServerConnection) -> None:
        "A client went: each program whose session its terminal carried hears so."
        self._held.discard(connection)
        for key, session in list(self._sessions.items()):
            if session.connection is connection:
                del self._sessions[key]
                self._tell(session.pane, TransferCommand(Action.STATUS, id=key[1], status=_TERMINAL_GONE))

    def _prune(self, now: float) -> None:
        for key, session in list(self._sessions.items()):
            if now - session.used > IDLE_SECONDS:
                del self._sessions[key]

    def _send(self, session: _Session, command: TransferCommand) -> None:
        "Give one command to whoever answers the session now: a transfer waits for no frame."
        connection = session.connection
        if session.route is Route.PEER:
            if connection.peer is not None:
                connection.peer.pane_transfer(session.pane, command.param())
            return
        if session.route is Route.TERMINAL:
            if connection.client is not None:
                connection.client.write_output_raw(str(command))
                connection.client.flush_output()
            return
        with suppress(BrokenPipeError):
            connection.send_packet(
                {
                    Field.CMD: Packet.KITTY_TRANSFER,
                    Field.DATA: command.param(),
                    Field.PANE: "%%%d" % (session.pane.pane_id,),
                }
            )

    def _tell(self, pane: Pane, answer: StringReport) -> None:
        "Write one command to a pane's program. A program that has gone hears nothing. A mirrored pane's goes to its owner."
        program = pane.process
        if isinstance(program, RemoteProgram):
            if isinstance(answer, OscString):
                program.kitty_transfer(answer.payload)
            elif isinstance(answer, TransferCommand):
                program.kitty_transfer(answer.param())
            return
        screen = pane.screen
        if not isinstance(screen, Screen):
            return
        try:
            screen.reply(answer)
        except OSError:
            logger.debug("Pane %s went before its file transfer command.", pane.pane_id)
