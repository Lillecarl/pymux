import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.format import format_pymux_string


def display_message(pymux: "Pymux", args: argparse.Namespace) -> None:
    '''
    Show a message on the status line.

    With `-p`, print the message, formatted, instead: the way a script
    asks the session a question and reads the answer. tmux spells it
    the same. Lillecarl/pymux#289.
    '''
    message = args.message
    if args.p:
        answer(pymux, format_pymux_string(pymux, message))
        return

    client_state = pymux.get_client_state()
    client_state.message = message


def register(subparsers):
    parser = add_command(subparsers, display_message)
    parser.add_argument("-p", dest="p", action="store_true", help="Print the message instead of showing it.")
    parser.add_argument("message", metavar="<message>")
