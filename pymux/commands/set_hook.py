import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, add_command


def set_hook(pymux: "Pymux", args: argparse.Namespace) -> None:
    """
    Run a command when the event it is named for happens.

    `set-hook after-select-pane display-panes` runs display-panes
    every time a pane is selected. The names are tmux's:
    `after-split-window`, `after-new-window`, `after-break-pane`,
    `after-respawn-pane`, `pane-died`, `client-attached`,
    `client-detached`, and `after-<name>` for every command, which
    is how the wake of a command that ran is spelled.

    A hook holds the commands it was given, in order; -u forgets the
    hook and everything it was given. -g is accepted for tmux and
    changes nothing: there is one session per server, so every scope
    is the global one. Lillecarl/pymux#297.
    """
    if args.u:
        pymux.hooks.pop(args.hook, None)
        return

    if args.hook_command is None:
        raise CommandException("A hook runs a command; name one.")

    pymux.hooks.setdefault(args.hook, []).append(args.hook_command)


def register(subparsers):
    parser = add_command(subparsers, set_hook)
    parser.add_argument("-g", dest="g", action="store_true", help="Accepted for tmux and changes nothing: there is one session per server.")
    parser.add_argument("-u", dest="u", action="store_true", help="Forget the hook, and everything it was given.")
    parser.add_argument("hook", metavar="<hook>", help="The event to run on.")
    parser.add_argument("hook_command", metavar="<command>", nargs="?")
