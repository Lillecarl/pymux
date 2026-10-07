from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer
from pymux.jobs import Job, JobId, decode


def show_job(pymux: Pymux, args: argparse.Namespace):
    """
    Show what a job said.

    The tail of one stream, decoded, with a note saying how much fell
    off past the cap. It reads a job that still runs as well as one
    that ended: the pumps fill the tail from the start, so this is
    how an agent tails a long job between polls of `list-jobs`.
    """
    job = pymux.jobs.get(JobId(args.job))
    if job is None:
        raise CommandException("no job %d" % args.job)
    answer(pymux, _stream(job, "stderr" if args.e else "stdout"))


def _stream(job: Job, stream: str) -> str:
    text = decode(job.kept(stream))
    if job.dropped(stream):
        text += "%s(%s dropped %d bytes past the cap)" % (
            "\n" if text else "",
            stream,
            job.dropped(stream),
        )
    return text or "(no output)"


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_job, name="show-job", read_only=True)
    parser.add_argument(
        "-e",
        dest="e",
        action="store_true",
        help="Show stderr. Stdout otherwise.",
    )
    parser.add_argument("job", metavar="<job>", type=int, help="The id `run` answered with.")
