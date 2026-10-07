from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, is_agent, refuse_without_a_waiter
from pymux.jobs import find_job

#: How long a tail follows when nobody said: a person at a keyboard
#: gets a patient default, and an agent names its own -- a wait
#: without one refuses under an agent session.
DEFAULT_TIMEOUT = 90


def tail_job(pymux: Pymux, args: argparse.Namespace):
    """
    Replay a job's tail and follow it live, and answer its exit.

    The replay is the last `--lines` of stdout, nothing when zero;
    the follow feeds both streams in the order they arrive until the
    job ends or `--timeout` passes. The command line reads the exit
    code: the job's own when it ends, 124 past the timeout with the
    job still running, 4 when no job answers. A timeout leaves the
    job running -- tail it again.
    """
    refuse_without_a_waiter(pymux, "wait")
    try:
        job = find_job(pymux, args.job, args.tags, args.session)
    except CommandException:
        pymux.command_exit_code = 4
        raise
    timeout = args.timeout
    if timeout is None:
        if is_agent(pymux):
            raise CommandException("tail without --timeout waits without bound, which an agent refuses: pass one.")
        timeout = DEFAULT_TIMEOUT
    if timeout < 0:
        raise CommandException("a timeout counts seconds, so it cannot be negative.")

    async def until_it_ends() -> None:
        pymux.command_exit_code = await pymux.jobs.follow(job, args.lines, timeout, lambda text: answer(pymux, text))

    return until_it_ends()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, tail_job, name="tail")
    parser.add_argument("job", metavar="<job>", type=int, nargs="?", help="The id `run` answered with.")
    parser.add_argument(
        "--tag",
        "-T",
        dest="tags",
        action="append",
        default=[],
        metavar="<key[=value]>",
        help="Tail the newest job carrying every tag, instead of an id.",
    )
    parser.add_argument(
        "--session",
        metavar="<id>",
        default=None,
        help="Scope to this session only. Without it the caller's session sorts first and the rest is fallback.",
    )
    parser.add_argument(
        "--lines",
        "-n",
        dest="lines",
        metavar="<lines>",
        type=int,
        default=10,
        help="Replay this many lines of stdout first. Zero follows only.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        metavar="<seconds>",
        type=float,
        default=None,
        help="Follow this long, then stop with 124 and the job still running. Zero replays only. Required under an agent session.",
    )
