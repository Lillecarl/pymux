from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer
from pymux.jobs import describe


def list_jobs(pymux: Pymux, args: argparse.Namespace):
    """
    List the jobs the server remembers.

    One line each, oldest first: the id `run` answered with, whether
    it still runs or what it exited with, how long it ran, and the
    command. A person watches the table fill; an agent polls for the
    line that says its job ended.
    """
    lines = [describe(job) for job in pymux.jobs.listing()]
    answer(pymux, "\n".join(lines) if lines else "(no jobs)")


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, list_jobs, name="list-jobs", read_only=True)
