import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import answer, the_pane


def list_user_vars(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per user variable of a pane: the name and the value.

    A shell publishes these with "OSC 1337 ; SetUserVar", and this
    reads them back: the git branch, the venv, whatever the program
    named. The active pane answers when no target names one.
    """
    pane = the_pane(pymux, args.target_pane)
    lines = [
        "%s=%s" % (name, value) for name, value in sorted(pane.user_vars.items())
    ]
    answer(pymux, "\n".join(lines))


def register(subparsers):
    parser = add_command(subparsers, list_user_vars)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane whose variables to list.")
