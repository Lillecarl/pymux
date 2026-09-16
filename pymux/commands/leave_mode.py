import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException
from pymux.commands import add_command


def leave_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Leave the innermost mode, and put back what entering it changed.

    Lillecarl/pymux#394.
    """
    try:
        pymux.key_bindings_manager.leave_mode()
    except IndexError:
        raise CommandException("no mode to leave")
    except ValueError:
        raise CommandException("no client attached: a mode belongs to a client")


def register(subparsers):
    add_command(subparsers, leave_mode)
