"""
`wait-pane`: hold until a pane meets a condition. Lillecarl/pymux#388.
"""

from __future__ import annotations

import sys

import anyio
import pytest

from pymux.answer import Answer
from pymux.commands import call_command_handler
from pymux.commands.wait_pane import TIMED_OUT
from pymux.core.main import Pymux

COLUMNS = 30
LINES = 5

#: A program that writes nothing and runs until it is killed.
IDLE = "%s -c 'import sys; sys.stdin.read()'" % (sys.executable,)


@pytest.fixture
async def pymux():
    mux = Pymux()
    async with mux.running():
        await mux.create_window(IDLE)
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


def the_pane(mux):
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    return pane


def arrives(pane, data: str) -> None:
    "Output, with the change event the pty fires after each read."
    pane.terminal.terminal_control.stream.feed(data)
    pane.terminal.terminal_control.on_content_changed.fire()


def started(mux, pane, *arguments: str):
    """
    The wait, started and not yet awaited, and the answer it writes to.

    The condition is read when the command runs, so a test that feeds
    the pane afterwards feeds what the wait has to see change.
    """
    answer = mux.answer = Answer()
    try:
        waiting = call_command_handler("wait-pane", mux, ["-t", "%%%i" % pane.pane_id, *arguments])
    finally:
        mux.answer = None
    assert waiting is not None, "wait-pane has to answer a coroutine"
    return waiting, answer


async def waited(mux, pane, *arguments: str) -> Answer:
    "A wait that is expected to answer from what the pane holds now."
    waiting, answer = started(mux, pane, *arguments)
    mux.answer = answer
    try:
        with anyio.fail_after(2):
            await waiting
    finally:
        mux.answer = None
    return answer


async def while_feeding(mux, waiting, answer: Answer, feed) -> Answer:
    "Await `waiting` while `feed` writes to the pane."
    mux.answer = answer
    try:
        with anyio.fail_after(3):
            async with anyio.create_task_group() as group:
                group.start_soon(lambda: waiting)
                await feed()
    finally:
        mux.answer = None
    return answer


# ----------------------------------------------------------------------
# --text


async def test_text_on_the_screen_answers_at_once(pymux):
    pane = the_pane(pymux)
    arrives(pane, "build ok\r\n")

    answer = await waited(pymux, pane, "--text", "ok$")

    assert answer.output == ["build ok"]
    assert answer.exit_code in (None, 0)


async def test_text_that_arrives_ends_the_wait(pymux):
    pane = the_pane(pymux)
    waiting, answer = started(pymux, pane, "--text", "done")

    async def feed() -> None:
        await anyio.sleep(0.05)
        arrives(pane, "working\r\n")
        await anyio.sleep(0.05)
        arrives(pane, "all done\r\n")

    await while_feeding(pymux, waiting, answer, feed)

    assert answer.output == ["all done"]


# ----------------------------------------------------------------------
# --next-text


async def test_next_text_does_not_match_what_is_already_there(pymux):
    "The echo of the command line is on the screen before the output."
    pane = the_pane(pymux)
    arrives(pane, "$ grep done log\r\n")
    waiting, answer = started(pymux, pane, "--next-text", "done")

    async def feed() -> None:
        await anyio.sleep(0.05)
        arrives(pane, "nothing yet\r\n")
        await anyio.sleep(0.05)
        arrives(pane, "job done\r\n")

    await while_feeding(pymux, waiting, answer, feed)

    assert answer.output == ["job done"]


async def test_next_text_does_not_match_a_screen_that_comes_back(pymux):
    """
    Leaving the alternate screen writes every row again, and the rows
    that come back are the old ones.
    """
    pane = the_pane(pymux)
    arrives(pane, "old done\r\n")
    waiting, answer = started(pymux, pane, "--next-text", "done")

    async def feed() -> None:
        await anyio.sleep(0.05)
        arrives(pane, "\x1b[?1049h")
        await anyio.sleep(0.05)
        arrives(pane, "\x1b[?1049l")
        await anyio.sleep(0.05)
        arrives(pane, "new done\r\n")

    await while_feeding(pymux, waiting, answer, feed)

    assert answer.output == ["new done"]


# ----------------------------------------------------------------------
# --quiet


async def test_quiet_waits_until_output_stops(pymux):
    pane = the_pane(pymux)
    waiting, answer = started(pymux, pane, "--quiet", "0.15")
    times: dict[str, float] = {}

    async def feed() -> None:
        for _ in range(6):
            arrives(pane, ".")
            await anyio.sleep(0.05)
        times["last"] = anyio.current_time()

    await while_feeding(pymux, waiting, answer, feed)
    times["answered"] = anyio.current_time()

    assert answer.output == []
    assert answer.exit_code in (None, 0)
    assert times["answered"] - times["last"] >= 0.1


async def test_a_quiet_longer_than_the_timeout_is_refused(pymux):
    pane = the_pane(pymux)
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    pymux.answer = Answer()
    try:
        call_command_handler("wait-pane", pymux, ["-t", "%%%i" % pane.pane_id, "--quiet", "2", "--timeout", "1"])
    finally:
        pymux.answer = None

    assert errors == ["pymux: A quiet of 2s never fits in a timeout of 1s."]


# ----------------------------------------------------------------------
# --exit


async def test_exit_answers_when_the_program_ends(pymux):
    pane = the_pane(pymux)
    pymux.options_of_pane(pane).remain_on_exit = True
    waiting, answer = started(pymux, pane, "--exit")

    async def feed() -> None:
        await anyio.sleep(0.1)
        pane.process.kill()

    await while_feeding(pymux, waiting, answer, feed)

    assert pane.process.is_terminated
    assert answer.exit_code in (None, 0)


# ----------------------------------------------------------------------
# send-keys --wait-*


async def test_send_keys_arms_the_wait_before_the_keys(pymux):
    """
    Output that lands before the wait first runs counts, because the
    mark is taken when the command runs and not when it is awaited.
    """
    pane = the_pane(pymux)
    arrives(pane, "ok from the last run\r\n")
    waiting, answer = started_by_send_keys(pymux, pane, "--wait-next-text", "^ok", "-l", "go")

    arrives(pane, "ok at once\r\n")
    pymux.answer = answer
    try:
        with anyio.fail_after(2):
            await waiting
    finally:
        pymux.answer = None

    assert answer.output == ["ok at once"]


async def test_send_keys_with_no_wait_answers_at_once(pymux):
    pane = the_pane(pymux)
    pymux.answer = Answer()
    try:
        assert call_command_handler("send-keys", pymux, ["-t", "%%%i" % pane.pane_id, "-l", "go"]) is None
    finally:
        pymux.answer = None


def started_by_send_keys(mux, pane, *arguments: str):
    answer = mux.answer = Answer()
    try:
        waiting = call_command_handler("send-keys", mux, ["-t", "%%%i" % pane.pane_id, *arguments])
    finally:
        mux.answer = None
    assert waiting is not None, "send-keys with a wait has to answer a coroutine"
    return waiting, answer


# ----------------------------------------------------------------------
# What every wait shares.


async def test_a_wait_that_runs_out_exits_124_with_the_screen(pymux):
    "So a failed wait says what the pane showed instead."
    pane = the_pane(pymux)
    arrives(pane, "still compiling\r\n")

    answer = await waited(pymux, pane, "--text", "done", "--timeout", "0.1")

    assert answer.exit_code == TIMED_OUT
    assert answer.output[0].splitlines()[0] == "still compiling"


async def test_a_wait_leaves_no_handler_behind(pymux):
    pane = the_pane(pymux)
    changed = pane.terminal.terminal_control.on_content_changed
    before = len(changed._handlers)

    for condition in (["--text", "x"], ["--next-text", "x"], ["--quiet", "0.05"], ["--exit"]):
        await waited(pymux, pane, *condition, "--timeout", "0.1")

    assert len(changed._handlers) == before


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--text", "("], "Expecting a regular expression: ( (missing ), unterminated subpattern at position 0)"),
        (["--quiet", "nope"], "Expecting a number of seconds: nope"),
        (["--exit", "--timeout", "0"], "A wait is longer than no time at all."),
    ],
)
async def test_what_cannot_be_read_is_refused(pymux, arguments, message):
    pane = the_pane(pymux)
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    pymux.answer = Answer()
    try:
        call_command_handler("wait-pane", pymux, ["-t", "%%%i" % pane.pane_id, *arguments])
    finally:
        pymux.answer = None

    assert errors == ["pymux: %s" % (message,)]


async def test_a_wait_from_nobody_is_refused(pymux):
    "A key binding that waited would leave a task nobody reads. Lillecarl/pymux#302."
    pane = the_pane(pymux)
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    answer = call_command_handler("wait-pane", pymux, ["-t", "%%%i" % pane.pane_id, "--exit"])

    assert answer is None
    assert errors == ["pymux: not able to wait"]
