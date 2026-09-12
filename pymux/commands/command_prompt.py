import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding.vi_state import InputMode
from pymux.commands import add_command
from pymux.commands.common import ask_the_person


def command_prompt(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Open the command line, or ask a question with a command behind it.
    """
    client_state = pymux.get_client_state()

    if args.command:
        # When a 'command' has been given.
        ask_the_person(
            pymux,
            args.message or "(%s)" % args.command.split()[0],
            args.command,
            args.default or "",
        )
        return

    # Show the ':' prompt.
    client_state.prompt_text = ""
    client_state.prompt_command = ""

    get_app().layout.focus(client_state.command_buffer)
    get_app().vi_state.input_mode = InputMode.INSERT


def register(subparsers):
    parser = add_command(subparsers, command_prompt)
    parser.add_argument("-p", dest="message", metavar="<message>", help="The question to ask.")
    parser.add_argument("-I", dest="default", metavar="<default>", help="What the answer starts with.")
    parser.add_argument("command", nargs="?", metavar="<command>")
