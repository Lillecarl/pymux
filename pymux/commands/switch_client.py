import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.sessions import move_this_client, this_client


def switch_client(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this client to another session of this server.

    `-t` names one. `-n` and `-p` step along the sessions in the order
    they were made, and `-l` goes back to the one this client was on
    before.
    """
    target = args.target_session

    if args.n or args.p or args.l:
        client_state = this_client(pymux)
        if client_state is None:
            raise CommandException(
                "no client to move: this command did not come from an attached client."
            )

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


def register(subparsers):
    parser = add_command(subparsers, switch_client)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to switch to.")
    parser.add_argument("-n", dest="n", action="store_true", help="The next session.")
    parser.add_argument("-p", dest="p", action="store_true", help="The previous session.")
    parser.add_argument("-l", dest="l", action="store_true", help="The session this client was on before.")
