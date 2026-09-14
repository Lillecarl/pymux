import os
import signal
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

    #: What `pymux attach -n` called this terminal, when it named one.
    #: It is announced as a client option like any other, after the
    #: ones the configuration file gave, so the flag wins: a file is
    #: one machine's and a flag is this terminal's.
    #: Lillecarl/pymux#340.
    chosen_name: str | None = None

    #: Whether `pymux attach -x` asked for the other clients of the
    #: session to be hung up as well as detached. It rides on the
    #: `start-gui` packet, beside the flag for `-d`.
    #: Lillecarl/pymux#347.
    hang_up_others: bool = False

    #: Whether the server said to hang up as well as to leave. `attach
    #: -x` and `detach-client -P` are what say it.
    #: Lillecarl/pymux#347.
    hang_up_asked: bool = False

    def run_command(self, command, pane_id=None) -> int:
        """
        Ask the server to run this command. Return the exit code.
        """
        return 0

    def attach(self, detach_other_clients=False, color_depth=None):
        """
        Attach client user interface.
        """

    def hang_up_the_parent(self) -> None:
        """
        Send SIGHUP to the process that started this client, when the
        server asked for it.

        This is what `attach -x` adds to `attach -d`: the terminal that
        was opened to run pymux closes, rather than going back to a
        shell prompt nobody asked for. tmux does exactly this, in the
        client and after the terminal is back
        (`client.c:415`), and it skips the signal when the parent is
        init -- a client whose parent already went would otherwise
        hang up a process that is not the one it means.
        Lillecarl/pymux#347.

        **After the attachment, never during it.** A signal sent while
        the alternate screen is up leaves the terminal as pymux had it.
        """
        if not self.hang_up_asked:
            return

        # Windows has no SIGHUP and no process to send one to. The
        # client leaves, which is the rest of what was asked.
        if not hasattr(signal, "SIGHUP") or not hasattr(os, "getppid"):
            return

        parent = os.getppid()
        if parent <= 1:
            return

        try:
            os.kill(parent, signal.SIGHUP)
        except OSError:
            # The parent went between the question and the signal.
            # Nothing to hang up, and nothing to say about it.
            pass
