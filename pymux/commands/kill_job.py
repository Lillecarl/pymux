from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer
from pymux.jobs import JobId


def kill_job(pymux: Pymux, args: argparse.Namespace):
    """
    End a running job.

    Sends SIGTERM; a program that ignores it keeps running, and that
    is what `show-job` then says. There is no escalation on purpose:
    ending is one signal, and `wait-job` after it reports the exit
    the program chose. A job that already ended says so instead.
    """
    job = pymux.jobs.get(JobId(args.job))
    if job is None:
        raise CommandException("no job %d" % args.job)
    if not pymux.jobs.kill(job):
        raise CommandException("job %d already done (exit %s)" % (job.job_id, job.returncode))
    answer(pymux, "job %d stopping" % job.job_id)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, kill_job, name="kill-job")
    parser.add_argument("job", metavar="<job>", type=int, help="The id `run` answered with.")
