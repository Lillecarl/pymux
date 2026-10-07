from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import refuse_without_a_waiter
from pymux.commands.run import report_outcome
from pymux.jobs import Job, JobId


def wait_job(pymux: Pymux, args: argparse.Namespace):
    """
    Wait for a job to end, and answer with what it said.

    `run 'sleep 30'` says `1` and goes on; `wait-job 1` holds until
    the sleep ends and answers with the output, then fails when the
    exit is nonzero, the way `run -w` does. A job the table forgot
    says so instead of waiting forever.
    """
    refuse_without_a_waiter(pymux, "wait")
    job = _find(pymux, JobId(args.job))

    async def until_it_ends() -> None:
        await pymux.jobs.wait(job)
        report_outcome(pymux, job)

    return until_it_ends()


def _find(pymux: Pymux, job_id: JobId) -> Job:
    job = pymux.jobs.get(job_id)
    if job is None:
        raise CommandException("no job %d" % job_id)
    return job


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, wait_job, name="wait-job")
    parser.add_argument("job", metavar="<job>", type=int, help="The id `run` answered with.")
