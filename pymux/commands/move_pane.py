import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command
from pymux.commands.common import find_pane, find_window


def move_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move a pane into another window, beside or above the pane that is
    focused there.

    -s names the pane; the active one is the default. -t names the
    window the pane goes to; the active one is the default, and a
    window that holds the pane already answers nothing, the way
    tmux's does. -h lays the panes side by side and -v stacks them;
    -d leaves the focus where it was. A window the pane leaves empty
    is gone. Lillecarl/pymux#297.
    """
    if args.s:
        pane = find_pane(pymux, args.s)
        if pane is None:
            raise CommandException("Can't find pane: %s" % (args.s,))
    else:
        pane = pymux.arrangement.get_active_pane()

    if args.t:
        window = find_window(pymux, args.t)
        if window is None:
            raise CommandException("Can't find window: %s" % (args.t,))
    else:
        window = pymux.arrangement.get_active_window()

    if pymux._window_holding(pane) is window:
        return

    pymux.arrangement.move_pane_to_window(pane, window, vsplit=args.h)
    if not args.d:
        pymux.arrangement.set_active_window(window)


def add_arguments(parser):
    "What a command that moves a pane between windows takes."
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-v", dest="v", action="store_true", help="Stack the panes.")
    group.add_argument("-h", dest="h", action="store_true", help="Lay the panes side by side.")
    parser.add_argument("-d", dest="d", action="store_true", help="Leave the focus where it was.")
    parser.add_argument("-s", dest="s", metavar="<src-pane>", help="The pane to move.")
    parser.add_argument("-t", dest="t", metavar="<dst-window>", help="The window the pane goes to.")


def register(subparsers):
    add_arguments(add_command(subparsers, move_pane))
