import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def choose_window(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Show the windows of the session, to choose from.

    tmux spells the view choose-tree, and prefix w opens it there. A
    pymux server holds one session, so the tree has one root: the
    chooser lists the windows, `/` searches the names, and Enter
    switches to one. With a command as its argument, the chooser
    runs that command on the chosen window instead of switching to
    it, with `%%` in the command standing for the target of the
    window, the way the command-prompt does. Lillecarl/pymux#295.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing -- the same shape as the pop-ups.
    Lillecarl/pymux#272.
    """
    if pymux.command_output is not None:
        return
    pymux.get_client_state().layout_manager.display_chooser(
        template=args.command
    )


def register(subparsers):
    parser = add_command(subparsers, choose_window)
    parser.add_argument(
        "command",
        metavar="<command>",
        nargs="?",
        help="Run this command on the chosen window instead of switching to it. `%%` stands for the target of the window.",
    )
