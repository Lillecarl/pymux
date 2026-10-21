from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.application.current import set_app
from pyte.page import RowMark

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import send_key, terminal_of, the_pane
from pymux.key_spelling import event_however_it_is_written

#: The copy mode commands `send-keys -X` runs, by tmux's names, and
#: whether each goes down. `-o` after one jumps to where a command's
#: output starts instead of to a prompt. Lillecarl/pymux#500.
PROMPT_JUMPS = {"previous-prompt": False, "next-prompt": True}


def _copy_mode_command(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Run a copy mode command, in the copy mode of the client that asked.

    **Over the socket that is the client a person used last**, which is
    `copy-mode`'s answer too: copy mode is a client's own.
    """
    if not args.keys:
        raise CommandException("send-keys -X needs a command")
    name, *flags = args.keys
    down = PROMPT_JUMPS.get(name)
    if down is None:
        raise CommandException("unknown command: %s" % name)
    if flags not in ([], ["-o"]):
        raise CommandException("%s takes -o and nothing else" % name)

    client = pymux.the_client_to_tell()
    if client is None:
        raise CommandException("no client in copy mode")
    with set_app(client.app):
        view = terminal_of(the_pane(pymux, args.target_pane)).copy_view
        if view is None:
            raise CommandException("not in a mode")
        view.jump_to_mark(RowMark.OUTPUT if flags else RowMark.PROMPT, down)


def send_keys(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Send keys to a pane, as key names or as text.

    **The keys are optional, because `-R` needs none.** `send-keys -R`
    puts a pane back when a program has left it in a state a person
    cannot type out of, and asking for a key as well means sending one
    to a terminal that is being reset. tmux takes it on its own.
    Lillecarl/pymux#118.
    """
    if args.X:
        _copy_mode_command(pymux, args)
        return

    pane = the_pane(pymux, args.target_pane)
    terminal = terminal_of(pane)

    if args.R:
        # Reset the terminal of this pane. (Like `reset`.)
        #
        # `screen.reset()` makes a new page, so the cells go with it.
        # There was a call to `pt_screen.reset()` before this one, and
        # prompt_toolkit's `Screen` has no such method, so `send-keys
        # -R` raised `AttributeError` and reset nothing.
        # Lillecarl/pymux#118.
        terminal.screen.reset()

    keys = args.keys

    if args.l:
        # Send keys literally. (Don't interpret key names like 'Enter'.)
        terminal.process.write_input(" ".join(keys))
        return

    for key in keys:
        # Read the name into a key, in either spelling.
        try:
            event = event_however_it_is_written(key)
        except ValueError:
            # Not a known key name. Like tmux, send this argument as
            # literal text.
            terminal.process.write_input(key)
            continue

        send_key(pane, event, key)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, send_keys)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to send to.")
    parser.add_argument("-l", dest="l", action="store_true", help="Send the keys as text, not key names.")
    parser.add_argument("-R", dest="R", action="store_true", help="Reset the terminal of the pane first.")
    parser.add_argument(
        "-X",
        dest="X",
        action="store_true",
        help="Run a copy mode command: %s, each with -o for output." % ", ".join(PROMPT_JUMPS),
    )
    parser.add_argument("keys", nargs=argparse.REMAINDER, metavar="<keys>")
