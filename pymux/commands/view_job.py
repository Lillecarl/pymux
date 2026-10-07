from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_pane
from pymux.enums import Woke
from pymux.jobs import find_job


def view_job(pymux: Pymux, args: argparse.Namespace):
    """
    Replace a pane with a viewer for a job.

    The pane keeps its place and its size; its screen shows the tail
    of the job and follows the rest as it arrives, read-only, until
    the job ends and the pane says how. Closing the pane never touches
    the job, which runs on until it ends on its own. -t targets;
    without it the active pane. -k kills a program that still runs;
    without it a pane whose program is alive refuses, the same rule
    `respawn-pane` holds. `--tag` names the newest job carrying
    every tag instead of an id.
    """
    job = find_job(pymux, args.job, args.tags, args.session)

    pane = the_pane(pymux, args.target_pane)

    return _replace(pymux, pane, job, args)


async def _replace(pymux: Pymux, pane, job, args: argparse.Namespace) -> None:

    if pymux._window_holding(pane) is None:
        raise CommandException(
            "Can't view in a pane whose window is gone: a pane that ends leaves the tree, unless remain-on-exit holds it."
        )

    if not pane.process.is_terminated and not args.k:
        raise CommandException("Pane is busy: -k kills a program that runs.")

    pane.process.kill()
    new_pane = await pymux._create_pane(job=job)
    pymux.arrangement.replace_pane(pane, new_pane)
    pymux.invalidate(Woke.PANE_WAS_RESPAWNED)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, view_job, name="view-job")
    parser.add_argument("-k", dest="k", action="store_true", help="Kill a program that still runs.")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to replace.")
    parser.add_argument("job", metavar="<job>", type=int, nargs="?", help="The id `run` answered with.")
    parser.add_argument(
        "--tag",
        "-T",
        dest="tags",
        action="append",
        default=[],
        metavar="<key[=value]>",
        help="View the newest job carrying every tag, instead of an id.",
    )
    parser.add_argument(
        "--session",
        metavar="<id>",
        default=None,
        help="Scope to this session only. Without it the caller's session sorts first and the rest is fallback.",
    )
