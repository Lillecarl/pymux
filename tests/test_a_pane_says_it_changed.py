"""
"Has anything happened in this pane?", as one number to compare.

A caller that draws a pane -- a web UI, a script watching for output --
otherwise diffs captured text to find out. `#{pane_revision}` is an
integer instead, and `wait-pane-change` holds until it moves, so an idle
pane costs nothing at all rather than a round trip per tick.

**It only goes up, and it is to compare and not to order.** A step of
one says nothing about how much happened, and it moves on output that
draws nothing: `ptyhost` calls `invalidate` after every read whatever
the bytes were (`ptyhost/process.py`). That is the safe direction. A
reader that redraws for nothing loses a frame; one that misses a change
shows the wrong screen until the next one.

**It is not `screen.writes`.** That counts writes to rows, and stands
still through a DECSCNM and an "OSC 4" -- measured -- each of which
changes what a reader draws. A reader inside the server compares those
itself, the way `ptterm` compares reverse video. A reader holding only
this number cannot, so the number has to be the wider one.
Lillecarl/pymux#387.
"""

from __future__ import annotations

import sys

import anyio
import pytest

from pymux.commands import call_command_handler
from pymux.main import Pymux

COLUMNS = 20
LINES = 5


@pytest.fixture
def pymux():
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


def the_pane(mux):
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    return pane


def arrives(pane, data: str) -> None:
    """
    Output, the way it really arrives.

    `on_content_changed` is what the revision counts, and the pty fires
    it after every read. Feeding the stream alone would not, so a test
    that did would say nothing about the thing under test.
    """
    pane.terminal.terminal_control.stream.feed(data)
    pane.terminal.terminal_control.on_content_changed.fire()


def run(mux, command: str, *arguments) -> str:
    "A command that answers at once."
    mux.command_output = []
    try:
        answer = call_command_handler(command, mux, list(arguments))
        assert answer is None, "%s waits: use `waited` instead" % (command,)
        return "\n".join(mux.command_output)
    finally:
        mux.command_output = None


async def waited(mux, command: str, *arguments) -> str:
    """
    A command that answers later, awaited the way the server awaits it.

    `call_command_handler` hands back a coroutine and the server's own
    task awaits it (Lillecarl/pymux#87). A test that dropped it would
    leave the wait unrun and say nothing about it.
    """
    mux.command_output = []
    try:
        answer = call_command_handler(command, mux, list(arguments))
        if answer is not None:
            await answer
        return "\n".join(mux.command_output)
    finally:
        mux.command_output = None


def revision_of(mux, pane) -> str:
    return run(mux, "list-panes", "-F", "#{pane_revision}")


# ----------------------------------------------------------------------
# The number.


def test_a_fresh_pane_has_a_revision(pymux):
    pane = the_pane(pymux)
    assert pane.revision >= 0


def test_output_moves_it(pymux):
    pane = the_pane(pymux)
    before = pane.revision

    arrives(pane, "hello")

    assert pane.revision > before


def test_it_only_goes_up(pymux):
    "So a caller may keep the last one it saw and compare."
    pane = the_pane(pymux)
    seen = [pane.revision]
    for text in ["one", "\r\n", "\x1b[?1049h", "two", "\x1b[?1049l"]:
        arrives(pane, text)
        seen.append(pane.revision)

    assert seen == sorted(seen)
    assert seen[-1] > seen[0]


@pytest.mark.parametrize(
    ("name", "data"),
    [
        # Each of these leaves `screen.writes` where it was, so a
        # revision built on that counter would miss it.
        ("reverse video", "\x1b[?5h"),
        ("a palette colour", "\x1b]4;1;rgb:ff/55/55\x1b\\"),
        ("the foreground", "\x1b]10;#ff0000\x1b\\"),
    ],
)
def test_it_moves_for_what_the_row_counter_does_not_see(pymux, name, data):
    pane = the_pane(pymux)
    arrives(pane, "text")  # Something on the screen to change the look of.

    writes_before = pane.screen.writes
    before = pane.revision

    arrives(pane, data)

    assert pane.screen.writes == writes_before, (
        "%s was expected to leave the row counter alone; this test is "
        "about the revision being the wider number" % (name,)
    )
    assert pane.revision > before


def test_the_format_field_reads_it(pymux):
    pane = the_pane(pymux)
    arrives(pane, "hello")

    assert revision_of(pymux, pane) == str(pane.revision)


def test_two_panes_count_apart(pymux):
    pane = the_pane(pymux)
    window = pymux.arrangement.get_active_window()
    pymux.add_process("%s -c pass" % (sys.executable,), window=window)
    other = window.panes[-1]

    arrives(pane, "hello")

    assert other.revision != pane.revision


# ----------------------------------------------------------------------
# The wait.


async def test_a_pane_that_already_moved_answers_at_once(pymux):
    """
    The whole reason a caller may send a stale revision: it draws a
    frame, then waits, and anything that arrived in between is already
    accounted for.
    """
    pane = the_pane(pymux)
    stale = pane.revision
    arrives(pane, "hello")

    with anyio.fail_after(1):
        answer = await waited(
            pymux,
            "wait-pane-change",
            "-t",
            "%%%i" % pane.pane_id,
            "--since",
            str(stale),
        )

    assert answer == str(pane.revision)


async def test_a_wait_ends_when_the_pane_changes(pymux):
    pane = the_pane(pymux)
    pymux.command_output = []
    try:
        waiting = call_command_handler(
            "wait-pane-change", pymux, ["-t", "%%%i" % pane.pane_id, "--since", str(pane.revision)]
        )
        assert waiting is not None, "the command has to answer a coroutine"

        async with anyio.create_task_group() as group:

            async def wait() -> None:
                await waiting

            group.start_soon(wait)
            await anyio.sleep(0.05)
            arrives(pane, "hello")

        assert pymux.command_output == [str(pane.revision)]
    finally:
        pymux.command_output = None


async def test_a_wait_that_runs_out_answers_the_same_revision(pymux):
    "So a caller compares rather than trusting that something happened."
    pane = the_pane(pymux)
    standing = pane.revision

    with anyio.fail_after(2):
        answer = await waited(
            pymux,
            "wait-pane-change",
            "-t",
            "%%%i" % pane.pane_id,
            "--since",
            str(standing),
            "--timeout",
            "0.05",
        )

    assert answer == str(standing)


async def test_a_wait_leaves_no_handler_behind(pymux):
    """
    The handler sits on a terminal that outlives the command, so a
    server that kept them would call one handler per wait it had ever
    served. Three waits that all run out, and the count comes back.
    """
    pane = the_pane(pymux)
    changed = pane.terminal.terminal_control.on_content_changed
    before = len(changed._handlers)

    for _ in range(3):
        await waited(
            pymux,
            "wait-pane-change",
            "-t",
            "%%%i" % pane.pane_id,
            "--since",
            str(pane.revision),
            "--timeout",
            "0.05",
        )

    assert len(changed._handlers) == before


# Never awaiting the coroutine is the case under test, and python says
# so when it is collected. The saying is expected here and nowhere else.
@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited")
async def test_a_wait_nobody_awaits_hooks_nothing(pymux):
    """
    The reason the handler goes on inside the coroutine. A client that
    goes away between the command and the wait leaves the coroutine
    unawaited, and a handler hooked before it would stay for ever.
    """
    pane = the_pane(pymux)
    changed = pane.terminal.terminal_control.on_content_changed
    before = len(changed._handlers)

    pymux.command_output = []
    try:
        answer = call_command_handler(
            "wait-pane-change",
            pymux,
            ["-t", "%%%i" % pane.pane_id, "--since", str(pane.revision)],
        )
    finally:
        pymux.command_output = None

    assert answer is not None
    assert len(changed._handlers) == before
    answer.close()


def test_a_wait_from_nobody_is_refused(pymux):
    """
    `bind-key X wait-pane-change` would put a task in the server's group
    per press and nobody would read the answer. The same rule as
    `wait-for`. Lillecarl/pymux#302.
    """
    pane = the_pane(pymux)
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None

    # No `command_output`, which is what says a client is reading.
    answer = call_command_handler("wait-pane-change", pymux, ["-t", "%%%i" % pane.pane_id])

    assert answer is None
    assert errors == ["pymux: not able to wait"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--since", "nope"], "Expecting a revision: nope"),
        (["--timeout", "nope"], "Expecting a number of seconds: nope"),
        (["--timeout", "0"], "A wait is longer than no time at all."),
    ],
)
def test_what_cannot_be_read_is_refused(pymux, arguments, message):
    pane = the_pane(pymux)
    errors = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    pymux.command_output = []
    try:
        call_command_handler("wait-pane-change", pymux, ["-t", "%%%i" % pane.pane_id, *arguments])
    finally:
        pymux.command_output = None

    assert errors == ["pymux: %s" % (message,)]
