import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def confirm_before(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Ask on the command line before running a command.
    """
    client_state = pymux.get_client_state()

    client_state.confirm_text = args.message or ""
    client_state.confirm_command = args.command


def register(subparsers):
    parser = add_command(subparsers, confirm_before)
    parser.add_argument("-p", dest="message", metavar="<message>", help="The question to ask.")
    parser.add_argument("command", metavar="<command>", help="The command to run when the answer is yes.")
