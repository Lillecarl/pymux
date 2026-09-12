import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def copy_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Enter copy mode.
    """
    # TODO: handle '-u' (go in copy mode and page-up directly).

    pane = pymux.arrangement.get_active_pane()
    pane.enter_copy_mode()


def register(subparsers):
    parser = add_command(subparsers, copy_mode)
    parser.add_argument("-u", dest="u", action="store_true", help="Accepted for tmux. Pymux does not page up yet.")
