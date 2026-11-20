from __future__ import annotations

import argparse
import random
from typing import TYPE_CHECKING

import anyio

from pymux.commands import CommandParser, Target, add_command, add_target
from pymux.commands.common import send_key, terminal_of, the_pane
from pymux.keys.key_spelling import event_however_it_is_written
from pymux.keys.typing_pace import WORDS_PER_MINUTE, Pace, delays

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from typing import Any

    from pymux.core.main import Pymux
    from pymux.core.pane import Pane

ENTER = event_however_it_is_written("Enter")


def type_text(pymux: Pymux, args: argparse.Namespace) -> Coroutine[Any, Any, None]:
    """
    Type text into a pane at a person's pace, for a recording.

    Each character goes out on its own, after a wait that strays the way
    a person's does: quick inside a word, slower at a space and after a
    full stop, and a pause to read the line before a newline, which goes
    as Enter. `-w` is the speed in words per minute, and `-S` makes the
    rhythm repeat: the same text, speed and seed give the same delays.
    Lillecarl/pyterm#1064.

    The command answers when the last key is out. Over the socket it
    stops when its caller goes, and when the pane closes.
    """
    pane = the_pane(pymux, args.target_pane)
    terminal_of(pane)
    # argparse keeps the `--` that lets a text start with a dash.
    words = args.text[1:] if args.text[:1] == ["--"] else args.text
    text = " ".join(words)
    seed = args.seed if args.seed is not None else random.randrange(1 << 32)
    return typed(pymux, pane, text, delays(text, Pace(args.words_per_minute, seed)))


async def typed(
    pymux: Pymux,
    pane: Pane,
    text: str,
    waits: list[float],
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    for char, wait in zip(text, waits, strict=True):
        if wait:
            await sleep(wait)
        if pymux.session_holding(pane) is None or pane.process.is_terminated:
            return
        if char == "\n":
            send_key(pane, ENTER, "Enter")
        else:
            terminal_of(pane).process.write_input(char)


def words_per_minute(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("a speed is between 1 and 1000 words per minute")
    return value


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, type_text)
    add_target(parser, "-t", kind=Target.PANE, dest="target_pane", help="The pane to type into.")
    parser.add_argument(
        "-w",
        dest="words_per_minute",
        type=words_per_minute,
        default=WORDS_PER_MINUTE,
        metavar="<wpm>",
        help="Words per minute, %g unless this says otherwise." % WORDS_PER_MINUTE,
    )
    parser.add_argument("-S", dest="seed", type=int, metavar="<seed>", help="Repeat the rhythm of this seed.")
    parser.add_argument("text", nargs=argparse.REMAINDER, metavar="<text>")
