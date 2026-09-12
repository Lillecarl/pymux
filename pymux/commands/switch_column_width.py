import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command
from pymux.enums import Woke


def switch_column_width(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Give this column of the strip the next preset width.

    -p: the previous one instead.

    The presets are a third, a half and two thirds of the window, which
    are niri's own. This is the strip's answer to `resize-pane`: a
    person picks between a few widths that fit together rather than
    nudging a border. Lillecarl/pymux#198.

    No key is bound to it. tmux has no equivalent command, so there is
    nothing to keep for muscle memory and nothing to collide with, and
    which key it should be is a choice rather than a default.
    """
    window = pymux.arrangement.get_active_window()

    if not window.strip:
        raise CommandException(
            "This window is not a strip. `set-window-option strip on` first."
        )

    pane = window.active_pane
    if pane is None:
        raise CommandException("There is no pane here.")

    window.switch_column_width(pane, back=args.p)
    pymux.invalidate(Woke.COLUMN_CHANGED_WIDTH)


def register(subparsers):
    parser = add_command(subparsers, switch_column_width)
    parser.add_argument("-p", dest="p", action="store_true", help="The previous width instead.")
