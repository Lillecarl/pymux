from abc import ABC

from prompt_toolkit.output import ColorDepth

__all__ = [
    "Client",
]


class Client(ABC):
    def run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command. Return the exit code.
        """
        return 0

    def attach(self, detach_other_clients=False, color_depth=None):
        """
        Attach client user interface.
        """
