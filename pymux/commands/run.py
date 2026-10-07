from __future__ import annotations

import argparse
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, refuse_without_a_waiter
from pymux.jobs import Job, outcome_text


def run_job(pymux: Pymux, args: argparse.Namespace):
    """
    Run a command without a pane, and remember it as a job.

    The command runs through the shell with pipes instead of a
    terminal, the way `run-shell` runs one; but where `run-shell`
    shows the output once and forgets it, a job keeps the tail of
    both streams, the exit code and the times, until the table
    forgets it past its cap. The answer is the id: `run 'sleep 30'`
    says `1`, and `wait-job 1` holds until it ends. `-w` waits here
    instead and answers with the output, for the agent that submits
    one command and reads the result.
    """
    # The command is the rest of the line, whatever words it holds.
    # Several words go back together, with the quoting the shell
    # read, the way `run-shell` beside this reads them; but one word
    # is already a whole command line -- `run 'echo a; echo b'` --
    # and joining would quote it once more, so the shell would look
    # for a program by that whole name and answer 127.
    words = args.shell_command
    shell_command = words[0] if len(words) == 1 else shlex.join(words)
    if not shell_command:
        raise CommandException("nothing to run")

    return _submit(pymux, args, shell_command)


async def _submit(pymux: Pymux, args: argparse.Namespace, shell_command: str) -> None:
    job = await pymux.jobs.submit(shell_command, args.directory)

    if args.w:
        refuse_without_a_waiter(pymux, "wait")
        await _run_and_report(pymux, job)
        return

    # `spawn_command` refuses outside `running`, where nothing would
    # finish the supervise; `-w` awaits it here instead, so it runs
    # anywhere a waiter stands.
    pymux.spawn_command(pymux.jobs.supervise(job))
    answer(pymux, "%d" % job.job_id)


async def _run_and_report(pymux: Pymux, job: Job) -> None:
    await pymux.jobs.supervise(job)
    report_outcome(pymux, job)


def report_outcome(pymux: Pymux, job: Job) -> None:
    """
    Answer with what the job said, and fail when it failed.

    The output goes back on the channel the asker reads either way;
    a nonzero exit is a `CommandException` after it, so a command
    line that waited says why it failed the way a shell does.
    """
    answer(pymux, outcome_text(job))
    if job.error is not None:
        raise CommandException("job %d never started: %s" % (job.job_id, job.error))
    if job.returncode:
        raise CommandException("job %d exited %d" % (job.job_id, job.returncode))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, run_job, name="run")
    parser.add_argument(
        "-w",
        dest="w",
        action="store_true",
        help="Wait for the job and answer with its output, instead of answering with its id.",
    )
    parser.add_argument(
        "-d",
        dest="directory",
        metavar="<directory>",
        default=None,
        help="Run in this directory. The server's own otherwise.",
    )
    parser.add_argument(
        "shell_command",
        nargs=argparse.REMAINDER,
        metavar="<shell-command>",
        help="The command to run, through the shell.",
    )
