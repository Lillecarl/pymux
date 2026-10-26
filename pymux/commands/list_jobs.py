from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import add_tag_filter, answer
from pymux.jobs import describe
from pymux.tags import carries, parse_tag


def list_jobs(pymux: Pymux, args: argparse.Namespace):
    """
    List the jobs the server remembers.

    One line each: the caller's session first when a packet said who
    called, oldest first within and otherwise. A person watches the
    table fill; an agent polls for the line that says its job ended,
    and its own jobs are the first lines it reads. `--tag` keeps the
    jobs carrying every tag it names.
    """
    caller = pymux.caller_context
    hint = caller.session_id if caller is not None else None
    wanted = [parse_tag(word) for word in args.tags]
    jobs = sorted(
        (job for job in pymux.jobs.listing() if carries(job.tags, wanted)),
        key=lambda job: (hint is not None and job.session != hint, job.job_id),
    )
    lines = [describe(job) for job in jobs]
    answer(pymux, "\n".join(lines) if lines else "(no jobs)")


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, list_jobs, name="list-jobs", read_only=True)
    add_tag_filter(parser, "jobs")
