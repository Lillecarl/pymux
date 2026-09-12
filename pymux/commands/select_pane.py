import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_pane
from pymux.layout import focus_down
from pymux.layout import focus_left
from pymux.layout import focus_right
from pymux.layout import focus_up


def select_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Focus a pane beside this one, or rotate the panes of the window.
    """
    if args.pane_id:
        pane_id = args.pane_id
        w = pymux.arrangement.get_active_window()

        if pane_id == ":.+":
            w.focus_next()
        elif pane_id == ":.-":
            w.focus_previous()
        else:
            pane = find_pane(pymux, pane_id)
            if pane is None:
                raise CommandException("Can't find pane: %s" % (pane_id,))
            w.active_pane = pane

    elif args.l:
        pymux.arrangement.get_active_window().rotate(with_pane_after_only=True)

    else:
        if args.L:
            h = focus_left
        if args.U:
            h = focus_up
        if args.D:
            h = focus_down
        if args.R:
            h = focus_right

        h(pymux)


def register(subparsers):
    parser = add_command(subparsers, select_pane)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", dest="L", action="store_true", help="Focus the pane to the left.")
    group.add_argument("-R", dest="R", action="store_true", help="Focus the pane to the right.")
    group.add_argument("-U", dest="U", action="store_true", help="Focus the pane above.")
    group.add_argument("-D", dest="D", action="store_true", help="Focus the pane below.")
    group.add_argument("-l", dest="l", action="store_true", help="Rotate the panes of the window once.")
    group.add_argument("-t", dest="pane_id", metavar="<pane-id>", help="The pane to focus.")
