import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.sessions import move_this_client


def attach_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Attach the calling client to a session of this server.

    A client is attached already -- the words arrive over the socket of
    a server it is on -- so this moves it, which is what tmux's
    attach-session does for a client that is inside a session. Without
    `-t` it goes to the session a person looked at last.
    """
    move_this_client(pymux, args.target_session, detach_others=args.d)


def register(subparsers):
    parser = add_command(subparsers, attach_session)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to attach to.")
    parser.add_argument("-d", dest="d", action="store_true", help="Detach the other clients of that session.")
