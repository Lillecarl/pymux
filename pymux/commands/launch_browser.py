"""
`launch-browser` and `hold-browser`: the server's half of `pymux-browser`.
Lillecarl/pymux#653.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.browsers import BrowserRefused
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, find_pane


def launch_browser(pymux: Pymux, args: argparse.Namespace):
    """
    Start a browser for a program here to drive, on a client's machine.

    The client is the one a person used last, and it is asked first.
    The answer is "<port> <path> <launch> <remote>" once the browser
    listens: <remote> is "cdp" for a Chromium, "bidi" for a Firefox.
    "-c <launch>" is the answer of the question, and counts only when a
    person typed it. Lillecarl/pymux#653.
    """
    if args.c is not None:
        if not pymux.a_person_asked():
            raise CommandException("only a person answers the question")
        pymux.browsers.approve(args.c)
        return None
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    pane = find_pane(pymux, None)
    return _launch(pymux, arguments, "pane %%%d" % (pane.pane_id,) if pane is not None else "a program")


async def _launch(pymux: Pymux, arguments: list[str], label: str) -> None:
    try:
        launch = await pymux.browsers.launch(arguments, label)
    except BrowserRefused as refused:
        raise CommandException(str(refused))
    answer(pymux, "%d %s %s %s" % (launch.port, launch.path, launch.launch_id, launch.remote))


def hold_browser(pymux: Pymux, args: argparse.Namespace):
    """
    Wait until the browser of a launch ends.

    When this command's own connection closes first, the browser ends
    with it: that is how a killed `pymux-browser` closes its browser.
    """
    launch = pymux.browsers.launches.get(args.launch)
    if launch is None:
        raise CommandException("no browser %s" % (args.launch,))
    if args.stop:
        pymux.browsers.stop(args.launch)
        return None
    return _hold(pymux, args.launch, launch.ended)


async def _hold(pymux: Pymux, launch_id: str, ended: anyio.Event) -> None:
    try:
        await ended.wait()
    finally:
        pymux.browsers.stop(launch_id)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, launch_browser)
    parser.add_argument("-c", dest="c", metavar="<launch>", help="The answer to the question about a launch.")
    parser.add_argument("arguments", nargs=argparse.REMAINDER, metavar="<argument>", help="What to pass the browser.")

    parser = add_command(subparsers, hold_browser)
    parser.add_argument("-s", dest="stop", action="store_true", help="Stop the browser now instead.")
    parser.add_argument("launch", metavar="<launch>")
