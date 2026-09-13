import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.sessions import find_session


def kill_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill a session, and every pane in it.

    The clients on it move to the session a person looked at last. The
    last session to go stops the server, which is the way the last
    `tmux kill-session` ends its own.
    """
    pymux.kill_session(find_session(pymux, args.target_session))


def register(subparsers):
    parser = add_command(subparsers, kill_session)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to kill.")
