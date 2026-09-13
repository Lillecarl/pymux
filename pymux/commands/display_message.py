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

    The message is a format, the way every other message is: `#{...}`
    and a `{{ template }}` both answer, and the client it is shown to
    is the one it is drawn for. Lillecarl/pymux#334.

    With `-p`, print the message instead: the way a script asks the
    session a question and reads the answer. tmux spells it the same.
    Lillecarl/pymux#289.
    '''
    if args.p:
        answer(pymux, format_pymux_string(pymux, args.message))
        return

    # The client a person is looking at. A command typed in a pane runs
    # under a fake CLI that draws nothing, so this used to say the
    # message to nobody at all. Lillecarl/pymux#272.
    client_state = pymux.the_client_to_tell()
    if client_state is None:
        pymux.add_command_error("pymux: nobody is attached to show a message to.")
        return

    # **Formatted here, and for the client that is told.** The status
    # line drew whatever was typed, so `display-message '#{session_name}'`
    # answered with the format string. tmux expands it either way, and
    # a binding that asks the session something is the whole use of
    # this command. Lillecarl/pymux#334.
    client_state.message = format_pymux_string(
        pymux, args.message, session=client_state.session, client=client_state
    )


def register(subparsers):
    parser = add_command(subparsers, display_message)
    parser.add_argument("-p", dest="p", action="store_true", help="Print the message instead of showing it.")
    parser.add_argument("message", metavar="<message>")
