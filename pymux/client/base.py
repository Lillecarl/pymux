from abc import ABC


__all__ = [
    "Client",
]


class Client(ABC):
    #: The configuration file this client takes its own options out
    #: of. `run_pymux` sets it when `-f` named one; otherwise the
    #: client looks where the server looks, on this machine.
    #:
    #: **A client option is announced by the client that holds it.**
    #: Over SSH the server's configuration file is another machine's,
    #: and the terminal a person is sitting at is here.
    #: Lillecarl/pymux#223.
    config_file: str | None = None

    def run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command. Return the exit code.
        """
        return 0

    def attach(self, detach_other_clients=False, color_depth=None):
        """
        Attach client user interface.
        """
