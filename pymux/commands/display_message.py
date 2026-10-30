from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer, clients_named
from pymux.format import format_pymux_string


def display_message(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Show a message on the status line.

    The message is a format, the way every other message is: `#{...}`
    and a `{{ template }}` both answer, and the client it is shown to
    is the one it is drawn for. Lillecarl/pymux#334.

    With `-p`, print the message instead: the way a script asks the
    session a question and reads the answer. tmux spells it the same.
    Lillecarl/pymux#289.

    `-c` names the client, as tmux's does: shown to that one, or with
    `-p` printed as drawn for it. Its `client_` variables are what a
    status line reads there. Lillecarl/pymux#754.
    """
    named = clients_named(pymux, args.target_client)[0] if args.target_client is not None else None
    if args.p:
        session = named.session if named is not None else None
        answer(pymux, format_pymux_string(pymux, args.message, session=session, client=named))
        return

    # The client a person is looking at: a command typed in a pane runs
    # under a temporary client that draws nothing, and a message there
    # reaches nobody. Lillecarl/pymux#272.
    client_state = named or pymux.the_client_to_tell()
    if client_state is None:
        pymux.add_command_error("pymux: nobody is attached to show a message to.")
        return

    # **Formatted here, and for the client that is told.** The status
    # line drew whatever was typed, so `display-message '#{session_name}'`
    # answered with the format string. tmux expands it either way, and
    # a binding that asks the session something is the whole use of
    # this command. Lillecarl/pymux#334.
    client_state.message = format_pymux_string(pymux, args.message, session=client_state.session, client=client_state)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, display_message)
    parser.add_argument("-p", dest="p", action="store_true", help="Print the message instead of showing it.")
    parser.add_argument(
        "-c",
        dest="target_client",
        metavar="<target-client>",
        help="The client to show it to, or to print it as drawn for.",
    )
    parser.add_argument("message", metavar="<message>")
