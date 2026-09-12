import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.split_window import add_arguments, split_window


def new_pane(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Open a pane in a window.

    tmux has no new-pane; the name is here because a person thinking
    in panes reaches for it. A pane of a window is a split of it in
    pymux's tree, so a new pane splits the window: this is
    split-window under the name the ask means. Lillecarl/pymux#297.
    """
    split_window(pymux, args)


def register(subparsers):
    add_arguments(add_command(subparsers, new_pane))
