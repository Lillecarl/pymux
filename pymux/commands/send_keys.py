import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_pane
from pymux.commands.common import send_a_key
from pymux.key_spelling import event_however_it_is_written


def send_keys(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Send keys to a pane, as key names or as text.

    **The keys are optional, because `-R` needs none.** `send-keys -R`
    puts a pane back when a program has left it in a state a person
    cannot type out of, and asking for a key as well means sending one
    to a terminal that is being reset. tmux takes it on its own.
    Lillecarl/pymux#118.
    """
    if args.target_pane:
        pane = find_pane(pymux, args.target_pane)
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (args.target_pane,)
            )
    else:
        pane = pymux.arrangement.get_active_pane()

    if pane.is_copying:
        raise CommandException("Cannot send keys. Pane is in copy mode.")

    if args.R:
        # Reset the terminal of this pane. (Like `reset`.)
        #
        # `screen.reset()` makes a new page, so the cells go with it.
        # There was a call to `pt_screen.reset()` before this one, and
        # prompt_toolkit's `Screen` has no such method, so `send-keys
        # -R` raised `AttributeError` and reset nothing.
        # Lillecarl/pymux#118.
        pane.screen.reset()

    keys = args.keys

    if args.l:
        # Send keys literally. (Don't interpret key names like 'Enter'.)
        pane.process.write_input(" ".join(keys))
        return

    for key in keys:
        # Read the name into a key, in either spelling.
        try:
            event = event_however_it_is_written(key)
        except ValueError:
            # Not a known key name. Like tmux, send this argument as
            # literal text.
            pane.process.write_input(key)
            continue

        send_a_key(pane, event, key)


def register(subparsers):
    parser = add_command(subparsers, send_keys)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to send to.")
    parser.add_argument("-l", dest="l", action="store_true", help="Send the keys as text, not key names.")
    parser.add_argument("-R", dest="R", action="store_true", help="Reset the terminal of the pane first.")
    parser.add_argument("keys", nargs=argparse.REMAINDER, metavar="<keys>")
