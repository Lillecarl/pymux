import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.sessions import find_session


def rename_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Rename a session. Without `-t`, the one this client is on.
    """
    session = find_session(pymux, args.target_session)

    taken = pymux.get_session(args.name)
    if taken is not None and taken is not session:
        raise CommandException("duplicate session: %s" % (args.name,))

    session.name = args.name


def register(subparsers):
    parser = add_command(subparsers, rename_session)
    parser.add_argument("-t", dest="target_session", metavar="<target-session>", help="The session to rename.")
    parser.add_argument("name", metavar="<name>", help="The new name of the session.")
