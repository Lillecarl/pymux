import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def customize_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    The options of the session, in a box, to change.

    A row is an option and what it holds; the search of `/` narrows
    the rows, and taking a row asks on the prompt what the option
    should hold, with what it holds as the default answer -- which is
    the editing half of what tmux's customize-mode does, and the
    other half is the listing, which is what show-options reads.
    Lillecarl/pymux#297.
    """
    try:
        state = pymux.get_client_state()
    except ValueError:
        return  # A command from the command line: nobody to show it to.

    state.layout_manager.display_options_chooser()


def register(subparsers):
    add_command(subparsers, customize_mode)
