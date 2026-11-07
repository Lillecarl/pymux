from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux


from pymux.commands import CommandParser, Target, add_command, add_target
from pymux.commands.sessions import find_session


def kill_session(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Kill a session, and every pane in it.

    The clients on it move to the session a person looked at last. The
    last session to go stops the server, which is the way the last
    `tmux kill-session` ends its own.
    """
    pymux.panes.kill_session(find_session(pymux, args.target_session))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, kill_session, forwards=True)
    add_target(parser, "-t", kind=Target.SESSION, dest="target_session", help="The session to kill.")
