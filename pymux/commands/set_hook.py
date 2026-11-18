from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.options.options import a_file_speaks


def set_hook(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Run a command when the event it is named for happens.

    `set-hook after-select-pane display-panes` runs display-panes
    every time a pane is selected. The names are tmux's:
    `after-split-window`, `after-new-window`, `after-break-pane`,
    `after-respawn-pane`, `pane-died`, `client-attached`,
    `client-detached`, and `after-<name>` for every command, which
    is how the wake of a command that ran is spelled.

    A hook holds one command, which a later `set-hook` replaces; -a
    adds the command after the ones the hook holds, so it runs them in
    order. That is tmux's rule (`options_array_clear` in
    cmd-set-option.c), and tmux configurations write `set-hook -ga`.
    Lillecarl/pyterm#1031. -u forgets the hook and everything it was
    given. -g is accepted for tmux and
    changes nothing: there is one session per server, so every scope
    is the global one. Lillecarl/pyterm#299.
    """
    if args.u:
        pymux.hooks.forget(args.hook)
    elif args.hook_command is None:
        raise CommandException("A hook runs a command; name one.")
    else:
        if not args.a:
            pymux.hooks.forget(args.hook)
        pymux.hooks.add(args.hook, args.hook_command)
    # A hook a person changed keeps their commands over an upgrade, and
    # the file the new server reads says every other. Lillecarl/pyterm#970.
    marked = pymux.set_by_hand["hooks"]
    if a_file_speaks(pymux):
        marked.discard(args.hook)
    else:
        marked.add(args.hook)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, set_hook)
    parser.add_argument(
        "-g",
        dest="g",
        action="store_true",
        help="Accepted for tmux and changes nothing: there is one session per server.",
    )
    parser.add_argument("-a", dest="a", action="store_true", help="Add the command after the ones the hook holds.")
    parser.add_argument("-u", dest="u", action="store_true", help="Forget the hook, and everything it was given.")
    parser.add_argument("hook", metavar="<hook>", help="The event to run on.")
    parser.add_argument("hook_command", metavar="<command>", nargs="?")
