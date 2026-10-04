from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux import introspect
from pymux.commands import CommandParser, add_command


def dump_stacks(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Write down what this server is doing now, and say where.

    Every thread, every asyncio task and what each one waits for.
    `pymux/introspect.py` says why a server answers for itself, and what
    `SIGUSR1` gives instead when the loop is too wedged to read this.
    """
    path = introspect.write_dump(pymux)
    pymux.print_command_line(str(path))
    pymux.show_message("Wrote a dump to %s" % (path,))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, dump_stacks)
