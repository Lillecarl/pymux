from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_pane, send_key
from pymux.keys.key_spelling import event_however_it_is_written


def send_prefix(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Send the prefix on to the pane, so a program can read it.
    """
    pane = find_pane(pymux, None)
    if pane is None:
        raise CommandException("no current pane")

    # The prefix is held as prompt_toolkit names, because that is what
    # binds it, and those re-spell as chords: "c-b" is "ctrl+b". So the
    # one command that sends a key pymux keeps for itself goes the same
    # road as `send-keys`, and says the same thing when a pane cannot
    # hear it. Lillecarl/pyterm#239.
    manager = pymux.key_bindings_manager
    keys = manager.prefix2 if args.second else manager.prefix
    for key in keys or ():
        send_key(pane, event_however_it_is_written(key), key)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, send_prefix)
    parser.add_argument("-2", dest="second", action="store_true", help="Send `prefix2` in place of `prefix`.")
