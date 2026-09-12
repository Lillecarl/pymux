import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def choose_buffer(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Show the named buffers, to choose from.

    Enter makes the chosen buffer the session's one buffer, which is
    what `paste-buffer` pastes. The chooser runs the keys of the
    window chooser: `/` searches, j and k with the arrows move, q and
    Escape leave. Lillecarl/pymux#304.

    The command line has no view to open a chooser on, so an asker
    that reads stdout gets nothing. Lillecarl/pymux#272.
    """
    if pymux.command_output is not None:
        return
    pymux.get_client_state().layout_manager.display_buffer_chooser()


def register(subparsers):
    add_command(subparsers, choose_buffer)
