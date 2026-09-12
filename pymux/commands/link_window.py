import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.common import find_window


def _find_anywhere(pymux: "Pymux", target: str | None):
    """
    A window of the order, or one that unlink_window took out: the
    pen is the window's own place while it waits, and link-window is
    the command that reaches into it.
    """
    window = find_window(pymux, target)
    if window is None and target and target.startswith("@") and target[1:].isdigit():
        window_id = int(target[1:])
        for unlinked in pymux.arrangement._unlinked_windows:
            if unlinked.window_id == window_id:
                return unlinked
    return window


def link_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Put a window in the order at the index -t names.

    The window is one unlink_window took out, or one that is in the
    order already, which moves it: tmux's link-window and move-window
    answer the same ask here, because pymux holds one session and a
    window has nowhere else to come from. Without -t the window goes
    after the last one. Lillecarl/pymux#297.
    """
    window = _find_anywhere(pymux, args.s)
    if window is None:
        raise CommandException("Can't find window: %s" % (args.s,))

    index = int(args.t) if args.t else None
    pymux.arrangement.link_window(window, index)


def register(subparsers):
    parser = add_command(subparsers, link_window)
    parser.add_argument("-s", dest="s", metavar="<src-window>", help="The window to link; the active one is the default.")
    parser.add_argument("-t", dest="t", metavar="<dst-index>", help="The index to put it at.")
