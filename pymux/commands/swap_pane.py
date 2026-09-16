import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def swap_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Swap the active pane with the one above or below.
    """
    # -U takes the pane below and -D the one above: the pair is the
    # mirror of tmux's, which sends -U up. The inversion is deliberate
    # and recorded: tests/reference/tmux_compat/divergences.toml.
    # Lillecarl/pymux#400. -D used to pass neither flag, which
    # rotated every pane of the window instead of swapping one.
    pymux.arrangement.get_active_window().rotate(
        with_pane_before_only=args.D, with_pane_after_only=args.U
    )


def register(subparsers):
    parser = add_command(subparsers, swap_pane)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-D", dest="D", action="store_true", help="Swap with the pane above.")
    group.add_argument("-U", dest="U", action="store_true", help="Swap with the pane below.")
