"""
Reading a client's clipboard with an OSC 52 query. Lillecarl/pymux#383.

The answer is cut out of the client's input before the key parser sees
it, because that parser caps a string reply at a kilobyte: a longer
clipboard would reach the pane as keys.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from clipboard_terminal import ClipboardTerminal
from prompt_toolkit.data_structures import Size
from session import over_connection

from pymux.clipboard_reads import CLIPBOARD_READ_SECONDS

SIZE = Size(rows=24, columns=80)


@asynccontextmanager
async def attached(terminal: ClipboardTerminal, pymux=None):
    "A server with one client whose terminal is `terminal`."
    async with over_connection(pymux=pymux, read_packet=terminal) as session:
        state, _size = await session.attach("one", SIZE)
        terminal.typed = session.typed
        terminal.state = state
        yield state


async def test_the_terminal_answers_with_its_clipboard():
    terminal = ClipboardTerminal()
    terminal.selections["c"] = "from the desktop"
    async with attached(terminal) as state:
        assert await state.connection.clipboard.read("c") == "from the desktop"
        assert state.connection.clipboard.answers is True


async def test_an_empty_clipboard_answers_at_once_with_nothing():
    "'ESC ] 52 ; c ; BEL' is an answer: no wait, and the terminal is asked again."
    terminal = ClipboardTerminal()
    async with attached(terminal) as state:
        with anyio.fail_after(CLIPBOARD_READ_SECONDS / 2):
            assert await state.connection.clipboard.read("c") is None
        assert state.connection.clipboard.answers is True


async def test_the_primary_is_its_own_selection():
    terminal = ClipboardTerminal()
    terminal.selections["c"] = "clipboard"
    terminal.selections["p"] = "primary"
    async with attached(terminal) as state:
        assert await state.connection.clipboard.read("p") == "primary"


async def test_a_long_answer_in_pieces_arrives_whole():
    "Far past the parser's kilobyte, and split across many packets."
    terminal = ClipboardTerminal(chunk=997)
    terminal.selections["c"] = "0123456789" * 20_000
    async with attached(terminal) as state:
        assert await state.connection.clipboard.read("c") == "0123456789" * 20_000


async def test_keys_around_an_answer_still_reach_the_parser():
    async with attached(ClipboardTerminal()) as state:
        connection = state.connection
        connection.clipboard._asked = True
        assert connection.clipboard.take_answer("a\x1b]52;c;aGk=\x07b") == "ab"
        assert connection.clipboard.take_answer("x\x1b]52;c;aG") == "x"
        assert connection.clipboard.take_answer("k=\x1b\\y") == "y"


async def test_a_silent_terminal_is_asked_once():
    terminal = ClipboardTerminal(answers=False)
    async with attached(terminal) as state:
        assert await state.connection.clipboard.read("c") is None
        assert state.connection.clipboard.answers is False

        with anyio.fail_after(CLIPBOARD_READ_SECONDS / 2):
            assert await state.connection.clipboard.read("c") is None
        assert terminal.queries == 1
