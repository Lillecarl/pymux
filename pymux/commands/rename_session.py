import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def rename_session(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Rename this session.
    """
    pymux.session_name = args.name


def register(subparsers):
    parser = add_command(subparsers, rename_session)
    parser.add_argument("name", metavar="<name>", help="The new name of the session.")
