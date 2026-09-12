import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command
from pymux.commands.common import answer


def show_prompt_history(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    One line per thing the command line and the prompts took.

    Oldest first, the way the history walks with up. The history is
    the server's one `InMemoryHistory`, and both buffers of every
    client share it. Lillecarl/pymux#305.
    """
    answer(pymux, "\n".join(pymux.prompt_history.get_strings()))


def register(subparsers):
    add_command(subparsers, show_prompt_history)
