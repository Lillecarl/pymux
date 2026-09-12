import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from prompt_toolkit.history import InMemoryHistory
from pymux.commands import add_command


def clear_prompt_history(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Forget everything the command line and the prompts took.

    The history is replaced rather than emptied: a fresh one goes to
    the server and to the buffers of every client it holds, which is
    all the readers there are. Lillecarl/pymux#305.
    """
    pymux.prompt_history = InMemoryHistory()
    for client_state in pymux._client_states.values():
        client_state.command_buffer.history = pymux.prompt_history
        client_state.prompt_buffer.history = pymux.prompt_history


def register(subparsers):
    add_command(subparsers, clear_prompt_history)
