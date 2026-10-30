"""
`wait-pane`: hold until a pane meets a condition.

A script or an agent that sends keys and then sleeps guesses twice: how
long the program takes, and whether it is done. This holds until the
pane says so, in one round trip, and a wait that runs out answers the
screen it ran out on, so the failure carries its own diagnosis.

One condition per wait:

- `--quiet SECONDS`: nothing changed in the pane for that long. The wait
  that works when the final text is unknown -- a build, a test run.
- `--text REGEX`: a line of the visible screen matches.
- `--next-text REGEX`: a line that changed after the wait began matches.
  What the screen held before -- the result of the last run -- does not.
- `--exit`: the program in the pane ended.

**`--next-text` sees what changed after the wait began**, so a caller
arms it before the keys go out: `send-keys --wait-next-text` does. Two
round trips leave room for the output of a fast command to land between
them. The echo of the typed command is new too, so a pattern that the
command line also holds wants an anchor: `^DONE`, not `DONE`.

Lillecarl/pymux#388.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Awaitable, Callable, Coroutine
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import refuse_without_a_waiter, terminal_of
from pymux.commands.wait_pane_change import DEFAULT_TIMEOUT, pane_to_wait_on, timeout_of

if TYPE_CHECKING:
    from pyte.page import TextLine
    from pyte.screen import Screen

    from pymux.answer import Answer
    from pymux.core.main import Pymux
    from pymux.core.window_tree import Pane

#: The exit code of a wait that ran out, as `timeout(1)` and `tail` say it.
TIMED_OUT = 124


class _Changes:
    "A pane's changes, as something to await."

    def __init__(self) -> None:
        self._event = anyio.Event()

    def __call__(self, _sender: object) -> None:
        self._event.set()

    async def next(self) -> None:
        """
        The next change, or at once when one came since the last.

        The event is replaced after the wake and not before the check,
        so a change between a check and this call is not lost.
        """
        await self._event.wait()
        self._event = anyio.Event()


def _visible_lines(screen: Screen) -> list[TextLine]:
    page = screen.page
    if page is None:
        return []
    top = screen.line_offset
    return page.text_lines(top, top + screen.lines - 1, trailing_spaces=False)


def _screen_text(screen: Screen) -> str:
    return "\n".join(line.text for line in _visible_lines(screen))


def _matching(pattern: re.Pattern[str], lines: list[TextLine]) -> str | None:
    return next((line.text for line in lines if pattern.search(line.text)), None)


def _new_lines(screen: Screen) -> Callable[[], list[TextLine]]:
    """
    The visible lines written since now, as a function to ask later.

    A line counts when one of its rows was written after now and its
    text differs from the text it showed now. The second test is for a
    program that leaves the alternate screen: every row is written
    again, and the rows that come back are old.
    """
    mark = screen.writes
    before = {line.first: line.text for line in _visible_lines(screen)}

    def new_lines() -> list[TextLine]:
        written_at = screen.written_at
        everything_at = screen.everything_at
        return [
            line
            for line in _visible_lines(screen)
            if before.get(line.first) != line.text
            and any(written_at.get(row, everything_at) > mark for row in range(line.first, line.last + 1))
        ]

    return new_lines


async def _until(changes: _Changes, met: Callable[[], str | None]) -> str:
    while (found := met()) is None:
        await changes.next()
    return found


async def _until_quiet(changes: _Changes, seconds: float) -> str:
    while True:
        with anyio.move_on_after(seconds) as still:
            await changes.next()
        if still.cancelled_caught:
            return ""


def _pattern(given: str) -> re.Pattern[str]:
    try:
        return re.compile(given)
    except re.error as error:
        raise CommandException("Expecting a regular expression: %s (%s)" % (given, error))


def _quiet(given: str, timeout: float) -> float:
    try:
        seconds = float(given)
    except ValueError:
        raise CommandException("Expecting a number of seconds: %s" % (given,))
    if seconds <= 0:
        raise CommandException("A quiet is longer than no time at all.")
    if seconds >= timeout:
        raise CommandException("A quiet of %gs never fits in a timeout of %gs." % (seconds, timeout))
    return seconds


def _condition(
    args: argparse.Namespace, pane: Pane, screen: Screen, changes: _Changes, timeout: float
) -> Callable[[], Awaitable[str]]:
    "What the wait awaits: it answers the line to print once the condition holds."
    if args.quiet is not None:
        return partial(_until_quiet, changes, _quiet(args.quiet, timeout))

    if args.text is not None:
        on_screen = _pattern(args.text)

        def met() -> str | None:
            return _matching(on_screen, _visible_lines(screen))

    elif args.next_text is not None:
        coming = _pattern(args.next_text)
        new_lines = _new_lines(screen)

        def met() -> str | None:
            return _matching(coming, new_lines())

    else:
        process = pane.process

        def met() -> str | None:
            return "" if process.is_terminated else None

    return partial(_until, changes, met)


def wait_pane(pymux: Pymux, args: argparse.Namespace):
    """
    Hold until the pane meets the condition, then answer.

    `--text` and `--next-text` print the line that matched; `--quiet` and
    `--exit` print nothing. A wait that runs out exits 124 and prints the
    visible screen.
    """
    reply = refuse_without_a_waiter(pymux, "wait")
    return armed(pymux, reply, pane_to_wait_on(pymux, args.target_pane), args)


def asks_for_a_wait(args: argparse.Namespace) -> bool:
    "Whether `args` names a condition, as `add_condition_arguments` reads them."
    return args.quiet is not None or args.text is not None or args.next_text is not None or args.exit


def armed(pymux: Pymux, reply: Answer, pane: Pane, args: argparse.Namespace) -> Coroutine[Any, Any, None]:
    """
    The wait on `pane`, ready to await.

    **What `--next-text` counts as new is fixed here, and not when the
    coroutine first runs.** `send-keys` arms the wait, then sends, then
    hands the coroutine back: a mark taken later would count the echo
    as old.
    """
    timeout = timeout_of(args.timeout)
    terminal = terminal_of(pane)
    screen = terminal.screen

    changes = _Changes()
    holds = _condition(args, pane, screen, changes, timeout)
    changed = terminal.terminal_control.on_content_changed

    async def until_it_holds() -> None:
        # Hooked inside the coroutine, for the reason `wait-pane-change`
        # gives: a coroutine nobody awaits would leave it hooked.
        changed.add_handler(changes)
        try:
            with anyio.move_on_after(timeout):
                found = await holds()
                if found:
                    pymux.print_command_line(found)
                return
        finally:
            changed.remove_handler(changes)
        reply.exit_code = TIMED_OUT
        pymux.print_command_line(_screen_text(screen))

    return until_it_holds()


def add_condition_arguments(parser: argparse.ArgumentParser, prefix: str, required: bool) -> None:
    "The flags of a wait, each spelled after `prefix`: `wait-pane --quiet`, `send-keys --wait-quiet`."
    condition = parser.add_mutually_exclusive_group(required=required)
    condition.add_argument(
        "--%squiet" % prefix,
        dest="quiet",
        metavar="<seconds>",
        help="Until nothing changed in the pane for this long.",
    )
    condition.add_argument(
        "--%stext" % prefix,
        dest="text",
        metavar="<regex>",
        help="Until a line of the visible screen matches. Prints the line.",
    )
    condition.add_argument(
        "--%snext-text" % prefix,
        dest="next_text",
        metavar="<regex>",
        help="Until a line that changed after the wait began matches. Prints the line.",
    )
    condition.add_argument(
        "--%sexit" % prefix, dest="exit", action="store_true", help="Until the program in the pane ends."
    )
    parser.add_argument(
        "--%stimeout" % prefix,
        dest="timeout",
        metavar="<seconds>",
        help="Give up after this long: exit %i and print the screen. %g by default." % (TIMED_OUT, DEFAULT_TIMEOUT),
    )


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, wait_pane, name="wait-pane")
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to watch.")
    add_condition_arguments(parser, "", required=True)
