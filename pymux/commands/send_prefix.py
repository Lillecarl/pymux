import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import send_a_key
from pymux.key_spelling import event_however_it_is_written


def send_prefix(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Send the prefix on to the pane, so a program can read it.
    """
    pane = pymux.arrangement.get_active_pane()

    # The prefix is held as prompt_toolkit names, because that is what
    # binds it, and those re-spell as chords: "c-b" is "ctrl+b". So the
    # one command that sends a key pymux keeps for itself goes the same
    # road as `send-keys`, and says the same thing when a pane cannot
    # hear it. Lillecarl/pymux#237.
    for key in pymux.key_bindings_manager.prefix:
        send_a_key(pane, event_however_it_is_written(key), key)


def register(subparsers):
    add_command(subparsers, send_prefix)
