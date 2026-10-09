from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import set_app

from pymux.commands import CommandException, CommandParser, add_command, not_past_this_client, this_client
from pymux.commands.common import clients_named, the_window
from pymux.commands.sessions import find_session, move_this_client


def switch_client(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Move this client to another session of this server.

    `-t` names one. `-n` and `-p` step along the sessions in the order
    they were made, and `-l` goes back to the one this client was on
    before.

    `-c` moves the client of that name instead, as `list-clients` prints
    it, and `-t` may name a window as well as a session: tmux reads
    both. A script then moves a person's view without typing at it.
    """
    target = args.target_session

    if args.target_client is not None:
        found = clients_named(pymux, args.target_client)
        if not found:
            raise CommandException("can't find client: %s" % (args.target_client,))
        client_state = found[0]
        not_past_this_client(pymux, client_state is not this_client(pymux))
        if target is not None and (target.startswith("@") or ":" in target):
            window = the_window(pymux, target)
            session = pymux.session_of_window(window)
            if session is None:
                raise CommandException("can't find window: %s" % (target,))
            pymux.attach_client_to(client_state, session)
            with set_app(client_state.app):
                session.arrangement.set_active_window(window)
        else:
            pymux.attach_client_to(client_state, find_session(pymux, target))
        return

    if args.n or args.p or args.l:
        client_state = this_client(pymux)
        if client_state is None:
            raise CommandException("no client to move: this command did not come from an attached client.")

        if args.l:
            previous = client_state.previous_session
            if previous is None or previous not in pymux.sessions:
                raise CommandException("no last session")
            target = "$%s" % (previous.session_id,)
        else:
            sessions = pymux.sessions
            here = sessions.index(client_state.session)
            step = 1 if args.n else -1
            target = "$%s" % (sessions[(here + step) % len(sessions)].session_id,)

    move_this_client(pymux, target)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, switch_client, read_only=True)
    parser.add_argument(
        "-t", dest="target_session", metavar="<target-session>", help="The session, or a window of it, to switch to."
    )
    parser.add_argument(
        "-c", dest="target_client", metavar="<target-client>", help="The client to move, as list-clients names it."
    )
    parser.add_argument("-n", dest="n", action="store_true", help="The next session.")
    parser.add_argument("-p", dest="p", action="store_true", help="The previous session.")
    parser.add_argument("-l", dest="l", action="store_true", help="The session this client was on before.")
