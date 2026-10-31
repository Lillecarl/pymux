"""
`wait-pane-change`: hold until a pane shows something else.

"Has this pane changed yet?" answered by asking once rather than by
asking again and again. A caller that draws a pane -- a web UI, a script
watching for output -- polls `#{pane_revision}` otherwise, and a poll is
wrong twice over: it is late by half its period, and it costs a round
trip per pane per tick while nothing happens.

So the caller hands over the revision it last saw and this holds until
the pane leaves it. Lillecarl/pymux#387.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.core.main import Pymux
    from pymux.core.pane import Pane


import anyio

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import find_pane, refuse_without_a_waiter, terminal_of, the_pane

#: How long a wait runs when nobody says. Short enough that a caller
#: behind a proxy with its own idea of an idle connection gets an answer
#: before the proxy does something about it, and long enough that an
#: idle pane costs about nothing.
DEFAULT_TIMEOUT = 60.0


def wait_pane_change(pymux: Pymux, args: argparse.Namespace):
    """
    Hold until this pane shows something other than `--since`.

    Prints the revision the pane holds when this answers: a new one
    when something happened, and the same one back when the wait ran
    out. A caller compares it with what it sent and asks again.

    **The revision is to compare and not to order.** It only goes up,
    but a step of one says nothing about how much happened, and it moves
    on output that draws nothing at all. `Pane.revision` says why.

    With no `--since` it waits for the next change, whatever the pane
    holds now. That is the honest reading of "no opinion about the
    present", and it is what a caller that has just drawn a frame wants.
    """
    # Before anything is read, so a refusal leaves nothing behind. A key
    # binding that waited would put a task in the server's group per
    # press, and nobody would ever read the answer.
    refuse_without_a_waiter(pymux, "wait")

    pane = pane_to_wait_on(pymux, args.target_pane)

    seconds = timeout_of(args.timeout)

    # Read before any waiting, so that a pane which has already moved
    # past `--since` answers at once rather than after a round of the
    # loop. That is the whole reason a caller may send a stale number
    # without losing a frame.
    if args.since is not None and pane.revision != _since(args.since):
        pymux.print_command_line(str(pane.revision))
        return None

    woken = anyio.Event()

    def it_changed(_sender) -> None:
        woken.set()

    # `add_handler` and `remove_handler` rather than "+=" and "-=". The
    # second of those assigns to the name, which would make `changed` a
    # local of the coroutine and raise before it unhooked anything.
    changed = terminal_of(pane).terminal_control.on_content_changed

    async def until_it_changes() -> None:
        # **Hooked inside the coroutine, not before it.** A handler put
        # on the terminal by the part that runs synchronously stays there
        # for ever if nobody awaits this -- and nothing is lost by
        # waiting, because there is no await point between the two, so no
        # event can fire in between.
        changed.add_handler(it_changed)
        try:
            with anyio.move_on_after(seconds):
                await woken.wait()
        finally:
            # **Always, and by the same function object.** A wait that
            # ran out and a wait whose client went away both leave a
            # handler on a terminal that outlives them, and a server
            # would then call one handler per wait it had ever served.
            changed.remove_handler(it_changed)

        pymux.print_command_line(str(pane.revision))

    return until_it_changes()


def pane_to_wait_on(pymux: Pymux, target: str | None) -> Pane:
    "The pane `-t` names, or the active one."
    if not target:
        pane = find_pane(pymux, None)
        if pane is None:
            raise CommandException("no pane to wait on")
        return pane
    return the_pane(pymux, target)


def _since(given: str) -> int:
    try:
        return int(given)
    except ValueError:
        raise CommandException("Expecting a revision: %s" % (given,))


def timeout_of(given: str | None) -> float:
    if given is None:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(given)
    except ValueError:
        raise CommandException("Expecting a number of seconds: %s" % (given,))
    if seconds <= 0:
        raise CommandException("A wait is longer than no time at all.")
    return seconds


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, wait_pane_change)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to watch.")
    parser.add_argument(
        "--since",
        dest="since",
        metavar="<revision>",
        help="The revision last seen. Answers at once when the pane has left it.",
    )
    parser.add_argument(
        "--timeout",
        dest="timeout",
        metavar="<seconds>",
        help="Give up after this long and answer the revision the pane holds. %g by default." % (DEFAULT_TIMEOUT,),
    )
