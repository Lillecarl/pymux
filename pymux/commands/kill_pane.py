import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.commands.common import find_pane


def kill_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Kill a pane, or the active one.
    """
    if args.target_pane:
        pane = find_pane(pymux, args.target_pane)
        if pane is None:
            raise CommandException(
                "Can't find pane: %s" % (args.target_pane,)
            )
    else:
        pane = pymux.arrangement.get_active_pane()
    pymux.kill_pane(pane)


def register(subparsers):
    parser = add_command(subparsers, kill_pane)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to kill.")
