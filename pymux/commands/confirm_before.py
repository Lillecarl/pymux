import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def confirm_before(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Ask on the command line before running a command.
    """
    # The client a person is looking at, which is not the one that ran
    # this when the command came from a pane. A question on a fake CLI
    # is one nobody can answer, so the command never ran.
    # Lillecarl/pymux#272.
    client_state = pymux.the_client_to_tell()
    if client_state is None:
        pymux.add_command_error("pymux: nobody is attached to ask.")
        return

    client_state.ask(args.message or "", args.command)


def register(subparsers):
    parser = add_command(subparsers, confirm_before)
    parser.add_argument("-p", dest="message", metavar="<message>", help="The question to ask.")
    parser.add_argument("command", metavar="<command>", help="The command to run when the answer is yes.")
