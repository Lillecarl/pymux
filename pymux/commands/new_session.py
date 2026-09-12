import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import print_object_format


def new_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Create a new session.

    Pymux has one session per server. The session is created when the server
    starts, so this command can only fail with a duplicate session error,
    like tmux does when the session already exists.
    """
    session_name = args.session_name

    if session_name and session_name != pymux.session_name:
        raise CommandException("duplicate session: %s" % (session_name,))

    if args.P:
        window = pymux.arrangement.get_active_window()
        print_object_format(
            pymux, args.format, window=window, pane=window.active_pane
        )


def register(subparsers):
    parser = add_command(subparsers, new_session)
    parser.add_argument("-s", dest="session_name", metavar="<session-name>", help="The name of the session.")
    parser.add_argument("-d", dest="d", action="store_true", help="Do not attach.")
    parser.add_argument("-P", dest="P", action="store_true", help="Print information about the session.")
    parser.add_argument("-F", dest="format", metavar="<format>", help="The format to print with -P.")
