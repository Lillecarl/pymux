"""
What a connection over the socket asks of the server when it does not
attach: a command to run and answer, or a pane to stream as frames.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

from libpymux.agentic import caller_context
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from pyte.keep import Keep

from pymux.commands import CommandException
from pymux.commands.common import find_pane, terminal_of

from ..answer import Answer
from ..commands import handle_command
from ..ids import PaneId
from ..keys.actions import Origin, started_by
from ..pipes.base import BrokenPipeError
from .attach import create_app

if TYPE_CHECKING:
    from pymux.web.stream import PaneStream

    from .server import ServerConnection

__all__ = ["Requests"]


class Requests:
    """
    The command or the stream one connection asked for, and its answer.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "connection": Keep.REBUILT,
        "stream": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

        #: The pane this connection is streaming, when it asked for one.
        #: A connection streams at most one pane: a caller that watches
        #: two opens two, so one slow viewer never holds up another.
        #: Lillecarl/pymux#461.
        self.stream: PaneStream | None = None

    async def stream_pane(self, packet: dict[str, Any]) -> None:
        """
        Send a pane to this client as frames, until it goes away.

        **The frames are the protocol and this envelope is not.** A relay
        passes `data` on without reading it, so the packet names here stay
        ours to change. Lillecarl/pymux#461.
        """
        from pymux.web.stream import PaneStream

        target = packet.get(Field.PANE)
        pane = find_pane(self.connection.pymux, target) if target else None
        try:
            if pane is None:
                raise CommandException("can't find pane: %s" % (target,))
            terminal = terminal_of(pane)
        except CommandException as refused:
            await self.connection.write_packet({Field.CMD: Packet.ERR, Field.DATA: "%s\n" % (refused.message,)})
            await self.connection.write_packet({Field.CMD: Packet.EXIT, Field.CODE: 1})
            self.connection.close_connection()
            return

        async def send(frame: dict[str, Any]) -> None:
            await self.connection.write_packet({Field.CMD: Packet.STREAM_OUT, Field.DATA: json.dumps(frame)})

        self.stream = PaneStream(self.connection.pymux, pane, terminal, send, writable=bool(packet.get(Field.WRITABLE)))
        try:
            await self.stream.run()
        except BrokenPipeError:
            pass  # The viewer went away, which is how a stream usually ends.
        finally:
            self.stream = None
            self.connection.close_connection()

    async def run(self, packet: dict[str, Any]) -> None:
        """
        Execute a run command from the client.
        """
        temporary = self.connection.client_state is None

        if temporary:
            # A command over the socket has no client of its own: it runs
            # under a temporary client, on the window that holds the pane it
            # was typed in, which goes again once the command answered.
            pane_id = packet.get(Field.PANE_ID)
            pane = self.connection.pymux.panes_by_id.get(PaneId(int(pane_id))) if pane_id is not None else None

            # The session of the pane the command was typed in. Without
            # it the fake client lands on the session a person looked at
            # last, so `pymux new-window` typed in a pane of one session
            # would open the window in another. Lillecarl/pymux#323.
            session = self.connection.pymux.session_holding(pane) if pane is not None else None

            create_app(self.connection, start=False, session=session)
            client_state = self.connection.client_state
            if client_state is not None and pane is not None and session is not None:
                client_state.caller_pane_id = pane.pane_id
                with set_app(client_state.app):
                    session.arrangement.set_active_window_from_pane_id(pane.pane_id)

            # A command can be the first touch of a server nobody
            # attached to yet: the bindings and the first window it
            # would have gotten then arrive now. Later ones noop.
            # Under the temporary application, so the new pane focuses.
            if client_state is not None:
                with set_app(client_state.app):
                    await self.connection.pymux.startup()

        pymux = self.connection.pymux
        # Who sent this, and from where: the client resolves both fresh
        # on every invocation and says them in the packet, because the
        # server's own environment names nothing about the caller.
        # `libpymux/agentic.py` holds the names.
        pymux.answer = reply = Answer(caller=caller_context(packet.get(Field.ENVIRONMENT), packet.get(Field.CWD)))

        client_state = self.connection.client_state
        if client_state is None:
            self.connection.close_connection()
            return

        with set_app(client_state.app):
            try:
                # **The client waits and the server does not.** A
                # command that has to wait answers with a coroutine,
                # and this route is the one that awaits it: the person
                # who typed `pymux wait-for done` waits for it, while
                # every other client of this server keeps running.
                # Lillecarl/pymux#87.
                with started_by(Origin.SOCKET):
                    answer = handle_command(pymux, packet[Field.DATA])
                    if answer is not None:
                        await answer
            finally:
                # Send the output of the command back to the client, and
                # close the connection.
                pymux.answer = None

                try:
                    if reply.output:
                        await self.connection.write_packet(
                            {Field.CMD: Packet.OUT, Field.DATA: "\n".join(reply.output) + "\n"}
                        )
                    if reply.errors or reply.warnings:
                        said = [*reply.warnings, *reply.errors]
                        await self.connection.write_packet({Field.CMD: Packet.ERR, Field.DATA: "\n".join(said) + "\n"})
                    exit_code = reply.exit_code
                    if exit_code is None:
                        exit_code = 1 if reply.errors else 0
                    await self.connection.write_packet({Field.CMD: Packet.EXIT, Field.CODE: exit_code})
                except BrokenPipeError:
                    pass
                self.connection.close_connection()

    async def answer(self, exit_code: int) -> None:
        "Send the exit code of a command, for one that ends the server before it returns."
        with suppress(BrokenPipeError):
            await self.connection.write_packet({Field.CMD: Packet.EXIT, Field.CODE: exit_code})
