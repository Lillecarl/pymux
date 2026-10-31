from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.core.pane import Pane
    from pymux.wayland.wayland import Launch


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, the_pane


def wl(pymux: Pymux, args: argparse.Namespace):
    """
    Run a Wayland program whose first window takes this pane's place.

    The pane becomes a window pane, and the shell in it goes: when the
    window closes, the pane closes. Later windows of the program open
    beside it. A program that ends before it opens a window leaves the
    pane as it was, and `pymux wl` answers with what it wrote and exits
    with its code. Lillecarl/pymux#630.
    """
    argv = args.program
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise CommandException("nothing to run")
    if pymux.wayland.compositor is None:
        raise CommandException("no Wayland compositor runs: wayland-server is off, or pysmithay is not here")
    pane = the_pane(pymux, args.target_pane)
    caller = pymux.caller
    env = dict(caller.environment) if caller is not None else dict(os.environ)
    cwd = (caller.cwd if caller is not None else None) or pane.current_directory
    return _run(pymux, pane, argv, cwd, env)


async def _run(pymux: Pymux, pane: Pane, argv: list[str], cwd: str | None, env: dict[str, str]) -> None:
    try:
        launch = await pymux.wayland.launch(pane, argv, cwd, env)
    except OSError as e:
        raise CommandException("%s: %s" % (argv[0], e.strerror or e))
    # A key binding has nobody to tell, and must not hold a task open.
    if pymux.answer is None:
        return
    await _opened_or_exited(launch)
    if launch.adopted is None and launch.exited.is_set():
        if launch.tail:
            answer(pymux, launch.tail.decode("utf-8", "replace").rstrip("\n"))
        pymux.answer.exit_code = launch.returncode or 0


async def _opened_or_exited(launch: Launch) -> None:
    async with anyio.create_task_group() as group:

        async def until(event: anyio.Event) -> None:
            await event.wait()
            group.cancel_scope.cancel()

        group.start_soon(until, launch.opened)
        group.start_soon(until, launch.exited)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, wl)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane the window takes.")
    parser.add_argument(
        "program",
        nargs=argparse.REMAINDER,
        metavar="<program>",
        help="The program to run, and its arguments.",
    )
