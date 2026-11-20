"""
`type-text` types into a pane at a person's pace, for a recording.

The delays are a pure function of the text, the speed and the seed, so
they are judged here without a clock; the command is judged with the
sleep replaced. Lillecarl/pyterm#1064.
"""

from __future__ import annotations

import contextvars
import statistics
import sys

import anyio
import pytest
from libpymux.wire import encode_json

from pymux.commands import call_command_handler, type_text
from pymux.commands.type_text import typed
from pymux.connection.server import ServerConnection
from pymux.core.main import Pymux
from pymux.keys.typing_pace import ENTER_GAP, WORD_GAP, Pace, delays
from pymux.pipes.memory import connect_in_memory

PROSE = "the quick brown fox jumps over the lazy dog. " * 20


def test_the_same_seed_types_the_same_rhythm():
    assert delays(PROSE, Pace(seed=7)) == delays(PROSE, Pace(seed=7))
    assert delays(PROSE, Pace(seed=7)) != delays(PROSE, Pace(seed=8))


def test_the_first_key_goes_at_once_and_one_wait_per_key():
    waits = delays("ls -la\n", Pace())
    assert waits[0] == 0.0
    assert len(waits) == len("ls -la\n")


def test_the_speed_is_the_words_per_minute_inside_a_word():
    "Letters after letters only, so no gap counts: their middle wait is the beat."
    pace = Pace(words_per_minute=60, seed=3)
    text = "a" + "bcdefghij" * 200
    middle = statistics.median(delays(text, pace)[1:])
    assert middle == pytest.approx(pace.beat, rel=0.1)


def test_a_person_waits_longer_at_a_word_and_longest_before_enter():
    pace = Pace(seed=1)
    text = "echo hello\n"
    waits = delays(text, pace)
    inside = max(waits[1:4])
    after_the_space = waits[text.index("h")]
    before_enter = waits[text.index("\n")]
    assert after_the_space > WORD_GAP[0] * pace.beat
    assert before_enter > ENTER_GAP[0] * pace.beat
    assert before_enter > after_the_space > 0
    assert inside < before_enter


def test_no_key_hangs():
    pace = Pace(seed=11)
    longest = max(delays(PROSE, pace))
    assert longest < 7 * pace.beat


@pytest.fixture
async def pymux():
    "A server with one window whose program waits, so the pane stays."
    mux = Pymux()
    async with mux.running():
        await mux.panes.create_window("%s -c 'import time; time.sleep(60)'" % (sys.executable,))
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


def create_pane(mux):
    pane = mux.arrangement.get_active_window().active_pane
    written: list[str] = []
    pane.process.write_input = written.append
    return pane, written


async def test_the_text_reaches_the_pane_with_enter_after_each_wait(pymux):
    pane, written = create_pane(pymux)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    text = "ls\nexit"
    waits = delays(text, Pace(seed=5))
    await typed(pymux, pane, text, waits, sleep)
    assert "".join(written) == "ls\rexit"
    assert slept == [wait for wait in waits if wait]


async def test_typing_stops_when_the_pane_closes(pymux):
    pane, written = create_pane(pymux)

    async def sleep(seconds: float) -> None:
        if len(written) == 2:
            call_command_handler("kill-pane", pymux, ["-t", "%%%d" % pane.pane_id])

    await typed(pymux, pane, "abcdef", delays("abcdef", Pace()), sleep)
    assert "".join(written) == "ab"


async def test_the_command_types_what_it_is_given(pymux):
    "At 200 words a minute, the wait is under a second."
    pane, written = create_pane(pymux)
    waiting = call_command_handler("type-text", pymux, ["-w", "200", "-S", "1", "echo", "hi\n"])
    assert waiting is not None
    await waiting
    assert "".join(written) == "echo hi\r"


async def test_a_text_may_start_with_a_dash(pymux):
    pane, written = create_pane(pymux)
    waiting = call_command_handler("type-text", pymux, ["-w", "1000", "--", "-la"])
    assert waiting is not None
    await waiting
    assert "".join(written) == "-la"


async def test_typing_stops_when_its_caller_goes(pymux, monkeypatch):
    """
    A command over the socket runs in its connection's task group, which
    is cancelled when the client goes. The cancel is the event: past it
    no key can go out.
    """
    pane, written = create_pane(pymux)
    first, cancelled = anyio.Event(), anyio.Event()

    def write_input(data: str) -> None:
        written.append(data)
        first.set()

    pane.process.write_input = write_input

    async def watched(*args, **kwargs) -> None:
        try:
            await typed(*args, **kwargs)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    monkeypatch.setattr(type_text, "typed", watched)
    server_end, client_end = connect_in_memory()
    connection = contextvars.copy_context().run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)
    # One word a minute: after the first key the next waits seconds.
    command = "type-text -t %%%d -w 1 abc" % pane.pane_id
    client_end.write_nowait(encode_json({"cmd": "run-command", "data": command}))
    with anyio.fail_after(10):
        await first.wait()
        client_end.close()
        await cancelled.wait()
    assert "".join(written) == "a"
