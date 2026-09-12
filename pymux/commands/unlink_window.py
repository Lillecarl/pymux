import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.common import find_window


def unlink_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Take a window out of the order, with its panes still running.

    tmux keeps an unlinked window for another session to take; pymux
    holds one session, so the window waits for link-window to put it
    back in the order, and no client can see it while it waits. The
    last window of the session refuses: there would be nothing left
    to watch. Lillecarl/pymux#297.
    """
    if args.target_window:
        window = find_window(pymux, args.target_window)
        if window is None:
            raise CommandException("Can't find window: %s" % (args.target_window,))
    else:
        window = pymux.arrangement.get_active_window()

    if len(pymux.arrangement.windows) == 1:
        raise CommandException("Can't unlink the last window.")

    pymux.arrangement.unlink_window(window)


def register(subparsers):
    parser = add_command(subparsers, unlink_window)
    parser.add_argument("-t", dest="target_window", metavar="<target-window>", help="The window to take out.")
