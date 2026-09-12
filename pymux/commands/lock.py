import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import add_command


def lock(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Cover the screen with the program `lock-command` names, and give
    it the keyboard until it is done. The overlay of `display-popup`
    is the thing that does that, full screen, and it closes itself
    when the program ends, which is the unlock.

    tmux has three of these: lock-client for the client that asked,
    lock-session for the clients of one session, lock-server for
    everything. A pymux overlay belongs to the session, so every
    client sees the same one, and the three arrive at the same
    screen. Lillecarl/pymux#297.
    """
    pymux.display_overlay(command=pymux.lock_command, width="100%", height="100%")
