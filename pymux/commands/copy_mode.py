from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from prompt_toolkit.application.current import set_app

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command


def copy_mode(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Enter copy mode, for the client that asked.

    **Over the socket it opens for the client a person used last**, as
    `display-message` shows there. The client a command arrives under
    then is temporary and draws nothing, and copy mode is a client's, so
    one opened for it is one nobody sees. Lillecarl/pymux#470.
    """
    # TODO: handle '-u' (go in copy mode and page-up directly).

    client = pymux.the_client_to_tell()
    if client is None:
        raise CommandException("no client to read the history")

    with set_app(client.app):
        pane = pymux.arrangement.get_active_pane()
        if pane is None:
            raise CommandException("no current pane")
        pane.enter_copy_mode()

        # Copy mode is a mode of its own, and its keys are the pane
        # widget's, under every table this client holds: a key table
        # left on the stack eats them. tmux's rule is one mode at a time.
        pymux.key_bindings_manager.leave_all_modes()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    # **`read_only`, as tmux marks it.** Copy mode is the client's and
    # reads a copy, so a person who only watches stops nothing for
    # anybody. Its keys are the copy buffer's and `may_type` does not
    # reach them, so that person can scroll and leave. tmux marks the
    # command and then drops every key of such a client, so a read-only
    # tmux client that enters is stuck. Lillecarl/pymux#470.
    parser = add_command(subparsers, copy_mode, read_only=True)
    parser.add_argument("-u", dest="u", action="store_true", help="Accepted for tmux. Pymux does not page up yet.")
