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

    `-d` takes the session from the other clients that hold it. `-x`
    does that and hangs up the process each of them was started by, so
    the terminal it was in closes rather than going back to a shell
    prompt. tmux spells both, and reads `-x` as `-d` with a harsher
    message. Lillecarl/pymux#347.
    """
    move_this_client(
        pymux, args.target_session, detach_others=args.d, hang_up_others=args.x
    )


def register(subparsers):
    parser = add_command(subparsers, attach_session)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to attach to.")
    parser.add_argument("-d", dest="d", action="store_true", help="Detach the other clients of that session.")
    parser.add_argument("-x", dest="x", action="store_true", help="Detach them, and hang up the process each one was started by.")
