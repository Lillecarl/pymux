import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.enums import Woke


def consume_or_expel(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Move this pane into the next column of the strip, or out of its own.

    -L: to the left. -R: to the right.

    **One key does both jobs.** A pane that shares its column leaves
    it, into a new column of its own on that side. A pane that is
    alone in its column joins the next column, at the bottom. niri
    binds this to one key each way, and the "or" is the point: a
    person holds the key and the pane walks in and out of the columns
    without deciding which move they wanted. Lillecarl/pymux#213.

    `move-column` moves a whole column along the row, and `break-pane`
    takes a pane out to a window of its own. This is the move between
    them, and it is the one a strip had no way to make: putting a pane
    in another column meant closing it and opening it again there.

    A pane that is alone in the column at the end of the row stays
    where it is. That is not an error.

    No key is bound to it. niri uses `Mod+BracketLeft` and
    `Mod+BracketRight`, and `{` and `}` are the same two keys behind
    the prefix, but tmux already binds those to `swap-pane -U` and
    `-D` and pymux keeps tmux's keys. Which keys the three commands of
    the strip take is Lillecarl/pymux#212.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    if window.consume_or_expel(pane, -1 if args.L else 1):
        pymux.invalidate(Woke.PANE_CHANGED_COLUMN)


def register(subparsers):
    parser = add_command(subparsers, consume_or_expel)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-L", dest="L", action="store_true", help="Move the pane into the column to the left.")
    group.add_argument("-R", dest="R", action="store_true", help="Move the pane into the column to the right.")
