from __future__ import annotations

import argparse
import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, refuse_without_a_waiter
from pymux.jobs import SESSION_TAG, Job, outcome_text, parse_env, parse_tag


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
    one command and reads the result. `--pty` runs the command on a
    terminal instead of pipes, for the command that needs one to
    behave; the waiting and the reading stay the same, with the whole
    terminal output in the stdout tail.
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
    caller = pymux.caller_context
    tags = dict(parse_tag(word) for word in args.tags)
    # An explicit session wins; the caller's stamps the rest, so its
    # later lookups prefer this job. A hand-typed session tag is left
    # alone: it filters like any other tag.
    if args.session is not None:
        tags[SESSION_TAG] = args.session
    elif SESSION_TAG not in tags and caller is not None and caller.session_id is not None:
        tags[SESSION_TAG] = caller.session_id
    # An explicit directory wins; the caller's is second, which is
    # where the command would have run had the caller run it itself.
    directory = args.directory or (caller.cwd if caller is not None else None)
    # The caller's environment, with the explicit variables last. A
    # key binding and an old client send no caller, and then the job
    # runs where the server stands, the way it always has.
    env = dict(caller.environment) if caller is not None else dict(os.environ)
    for word in args.env:
        key, value = parse_env(word)
        env[key] = value
    job = await pymux.jobs.submit(shell_command, directory, list(tags.items()), env, args.pty)

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
        help="Run in this directory. The caller's own otherwise, and the server's where no caller said.",
    )
    parser.add_argument(
        "--env",
        dest="env",
        action="append",
        default=[],
        metavar="<key=value>",
        help="Set a variable for the job, over the caller's environment. A bare key sets the empty string.",
    )
    parser.add_argument(
        "--tag",
        "-T",
        dest="tags",
        action="append",
        default=[],
        metavar="<key[=value]>",
        help="Tag the job: a bare key, or a key with a value. Repeat to tag it twice.",
    )
    parser.add_argument(
        "--session",
        metavar="<id>",
        default=None,
        help="Stamp this session onto the job instead of the caller's. Unstamped jobs belong to no session.",
    )
    parser.add_argument(
        "--pty",
        dest="pty",
        action="store_true",
        help="Run on a terminal instead of pipes, for a command that needs one. Waiting and reading stay the same.",
    )
    parser.add_argument(
        "shell_command",
        nargs=argparse.REMAINDER,
        metavar="<shell-command>",
        help="The command to run, through the shell.",
    )
