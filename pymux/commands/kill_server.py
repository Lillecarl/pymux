from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command

#: How long an attached client waits for the next server after
#: `kill-server -r`, in seconds.
RESTART_WAIT = 30.0


def kill_server(pymux: Pymux, args: argparse.Namespace):
    """
    Kill the server, and every session in it.

    `kill-session` takes one session; this takes them all, and the
    clients go with it.

    `-r` says a new server is coming on the same socket. Each attached
    client then waits for it, up to 30 seconds, instead of leaving.
    Lillecarl/pymux#409.
    """
    if not args.restarting:
        pymux.stop()
        return None

    async def tell_and_stop() -> None:
        # Refuse new clients first. A waiting client tries the socket at
        # once, and this server, still shutting down, would take it and
        # then end it with no restart announced.
        if pymux.listener is not None:
            pymux.listener.close()
        for connection in list(pymux.connections):
            client_state = connection.client_state
            if client_state is not None and not client_state.temporary:
                await connection.say_restarting(RESTART_WAIT)
        pymux.stop()

    return tell_and_stop()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, kill_server)
    parser.add_argument(
        "-r",
        dest="restarting",
        action="store_true",
        help="A new server follows on this socket: attached clients wait for it.",
    )
