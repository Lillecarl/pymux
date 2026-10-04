import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser
from pymux.commands import add_command
from pymux.commands.common import answer
from pymux.commands.common import the_pane


def get(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Read an id, by the thing it names.

    `get paneid` says the id of the pane a target names, or of the
    active one. The id is the handle every targeted command accepts
    as `%<id>`, and unlike the window and pane indexes it does not
    move when panes open and close. Lillecarl/pymux#291.
    """
    what = args.what
    if what != "paneid":
        raise CommandException("Unknown thing to get: %s" % (what,))

    pane = the_pane(pymux, args.target_pane)

    answer(pymux, str(pane.pane_id))


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    parser = add_command(subparsers, get)
    parser.add_argument(
        "-t", dest="target_pane",
        metavar="<target-pane>",
        help="The pane to read, rather than the active one.",
    )
    parser.add_argument("what", metavar="<what>")
