from __future__ import annotations

import argparse
import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command


def list_processes(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Name the processes that are pymux's own on this machine.

    One to a line: a role, then a pid. `server` is this process, `sound` each program sound runs, and
    `client` each client on this machine. A program a pane, a job or
    `run-shell` runs is a person's and is never listed. `pymux renice`
    reads this. Lillecarl/pymux#673.
    """
    here = socket.gethostname()
    lines = ["server %d" % os.getpid()]
    lines.extend("sound %d" % pid for pid in pymux.audio.pids())
    lines.extend("portal %d" % pid for pid in pymux.portal.pids())
    for connection in pymux.connections:
        # A client of another machine says a pid of that machine.
        if connection.pid and connection.hostname == here:
            lines.append("client %d" % connection.pid)
    for line in lines:
        pymux.print_command_line(line)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, list_processes, read_only=True)
