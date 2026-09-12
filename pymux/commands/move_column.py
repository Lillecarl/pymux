import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.enums import Woke


def move_column(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this column of the strip one place along the row.

    -L: to the left. -R: to the right.

    niri binds this beside the movement keys, and it is half of why
    the model works: a person puts related columns next to each other
    without renegotiating a layout. Without it, getting two columns
    adjacent means closing one and opening it again in the right
    place, which is the renegotiation a strip exists to avoid.
    Lillecarl/pymux#202.

    **The whole column moves**, panes and width and all. Taking the
    focused pane out of a stack is a different move, and `break-pane`
    is the command that does that kind of thing.

    A column at the end of the row stays where it is. That is not an
    error: a key held down at the edge of the row does nothing, the
    way it does nothing in niri.

    No key is bound to it, for the same reason as
    `switch-column-width`: tmux has no equivalent command, so there is
    nothing to keep for muscle memory and which key it should be is a
    choice rather than a default.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    if window.move_column(pane, -1 if args.L else 1):
        pymux.invalidate(Woke.COLUMN_MOVED)


def register(subparsers):
    parser = add_command(subparsers, move_column)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", dest="L", action="store_true", help="Move the column one place to the left.")
    group.add_argument("-R", dest="R", action="store_true", help="Move the column one place to the right.")
