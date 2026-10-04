import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command


def copy_mode(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Enter copy mode.
    """
    # TODO: handle '-u' (go in copy mode and page-up directly).

    pane = pymux.arrangement.get_active_pane()
    if pane is None:
        raise CommandException("no current pane")
    pane.enter_copy_mode()

    # Copy mode is a mode of its own, and its keys are the pane
    # widget's, under every table this client holds: a key table left
    # on the stack eats them. tmux's rule is one mode at a time.
    try:
        pymux.key_bindings_manager.leave_all_modes()
    except ValueError:
        # No client: a copy mode entered over the socket has nobody in
        # a mode to take out of one.
        pass


def register(subparsers: "argparse._SubParsersAction[CommandParser]"):
    # **Not `read_only`, although tmux marks it.** Copy mode belongs to
    # the pane, so a client that entered one stops the live screen for
    # everybody else watching it. And the keys that drive it are
    # prompt_toolkit's buffer keys, which `may_type` does not reach: a
    # read-only client that entered could not scroll and could not
    # leave. tmux marks the command and drops those keys, which is the
    # same trap. Lillecarl/pymux#467.
    parser = add_command(subparsers, copy_mode)
    parser.add_argument("-u", dest="u", action="store_true", help="Accepted for tmux. Pymux does not page up yet.")
