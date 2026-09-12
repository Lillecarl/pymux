import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import ask_the_person
from pymux.key_spelling import KeyCompleter


def compose_key(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Compose a key this keyboard cannot type, and send it to the pane.

    A laptop with no Home key, no Insert and no function row cannot
    answer a program that asks for one, and the fn chords differ per
    machine and per external keyboard. pymux is the layer in the
    middle, and it can send the key.

    The box completes the key names, so a person reads the list rather
    than remembering the spelling. "ctrl+home" and "C-Home" both read,
    and so does a sequence: "escape a" is two presses.
    Lillecarl/pymux#220.
    """
    ask_the_person(
        pymux,
        args.message or "Send key",
        "send-keys %%",
        args.default or "",
        # Not the prefix. It is a step of the grammar, and it is the
        # one key pymux keeps for itself; `send-prefix` sends it on.
        KeyCompleter(offer_the_prefix=False),
    )


def register(subparsers):
    parser = add_command(subparsers, compose_key)
    parser.add_argument("-p", dest="message", metavar="<message>", help="The question to ask.")
    parser.add_argument("-I", dest="default", metavar="<default>", help="What the answer starts with.")
