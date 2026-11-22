"""
A slow link carries the newest screen, and never a backlog.

Over ssh the frames a server sends wait in buffers nobody here can see:
sshd's, the kernel's, the link. A phone on a weak signal then shows
what was true seconds ago, and a keystroke's frame waits behind every
frame of an animation before it. So a client whose terminal answers a
status report gets one behind each frame, and the next frame waits for
the answer. Lillecarl/pyterm#261.

A terminal that never answered is never waited for, and a pane's
program never waits on any of it: pyte reads the pane whether anybody
draws or not.
"""

from __future__ import annotations

import gc
import itertools
import math
import time
from typing import NamedTuple

import anyio
from fairclock import Stopwatch
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.connection.pacing import STATUS_REPORT

SIZE = Size(rows=24, columns=80)
ANSWER = "\x1b[0n"


def frames_of(packets: list) -> list[str]:
    return [packet["data"] for packet in packets if packet.get("cmd") == "out" and packet.get("data")]


async def attach_paced(session, packets, paced=True):
    state, _ = await session.attach("phone", SIZE, paced=paced)
    await once(lambda: frames_of(packets), 5.0, "no frame arrived")
    return state


def redraw(state) -> None:
    state.app.invalidate()


async def test_a_paced_client_gets_a_report_behind_each_frame():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        await attach_paced(session, packets)

        assert all(frame.endswith(STATUS_REPORT) for frame in frames_of(packets))


async def test_a_client_that_did_not_ask_gets_none():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        await attach_paced(session, packets, paced=False)

        assert not any(STATUS_REPORT in frame for frame in frames_of(packets))


async def test_a_terminal_that_never_answered_is_not_waited_for():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        before = len(frames_of(packets))

        state.message = "first"
        redraw(state)
        await once(lambda: len(frames_of(packets)) > before, 5.0, "the second frame never came")

        assert not state.client.pacing.waits_for_terminal


async def answered_all(session, state, packets, in_flight: int = 1) -> None:
    """
    Every report answered, so the terminal is known to answer; `in_flight` frames allowed out.

    **Answer what arrived, until nothing is unread.** An answer can let
    a held frame go, and that frame carries a report of its own. Counting
    `unread` once answered the frames out then and left that one unread
    for ever, under load. The count here is of the reports that reached
    the client, so no report is answered twice. Lillecarl/pyterm#848.
    """
    connection = state.client.connection
    state.options.frames_in_flight = in_flight
    typed = 0

    def all_read() -> bool:
        nonlocal typed
        arrived = sum(frame.count(STATUS_REPORT) for frame in frames_of(packets))
        for _ in range(arrived - typed):
            session.typed(state, ANSWER)
        typed = arrived
        return connection.client.pacing.answers_reports and not connection.client.pacing.unread

    await once(
        all_read,
        5.0,
        # Built at the deadline, so it says what held then.
        lambda: (
            "answers not read: answers_reports=%r, unread=%r, typed %d"
            % (connection.client.pacing.answers_reports, list(connection.client.pacing.unread), typed)
        ),
    )


async def test_after_an_answer_the_next_frame_waits_for_the_next_one():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        connection = state.client.connection
        await answered_all(session, state, packets)

        state.message = "one"
        redraw(state)
        await once(lambda: len(connection.client.pacing.unread) == 1, 5.0, "the frame never went")
        sent = len(frames_of(packets))

        # Unanswered: more changes draw nothing.
        state.message = "two"
        redraw(state)
        state.message = "three"
        redraw(state)
        await once(lambda: connection.client.pacing.waits_for_terminal, 5.0, "the client was not held")

        # The answer draws the newest screen, once: a frame that went
        # while it was held would make the count overshoot.
        session.typed(state, ANSWER)
        await once(lambda: len(frames_of(packets)) > sent, 5.0, "the answer drew nothing")
        await once(lambda: connection.client.pacing.waits_for_terminal, 5.0, "the new frame was not held for")
        assert len(frames_of(packets)) == sent + 1
        assert "three" in frames_of(packets)[-1]


async def test_small_frames_go_until_the_count_is_out():
    """
    An animation runs faster than one frame a round trip: frames go
    until `frames-in-flight` are unanswered. Lillecarl/pyterm#827.
    """
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        connection = state.client.connection
        await answered_all(session, state, packets, in_flight=3)

        for word in ("one", "two", "three", "four"):
            state.message = word
            redraw(state)
            await once(
                lambda: connection.client.pacing.waits_for_terminal or word in frames_of(packets)[-1], 5.0, "no frame"
            )
        assert len(connection.client.pacing.unread) == 3
        assert connection.client.pacing.waits_for_terminal
        assert "four" not in frames_of(packets)[-1]

        # One answer makes room for the newest screen.
        session.typed(state, ANSWER)
        await once(lambda: "four" in frames_of(packets)[-1], 5.0, "the answer drew nothing")


async def test_a_frame_past_the_bytes_waits_however_few_frames():
    """
    What a key waits behind is bytes in the link, so a big frame holds
    the next one though the count has room. Lillecarl/pyterm#827.
    """
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        connection = state.client.connection
        await answered_all(session, state, packets, in_flight=5)
        state.options.bytes_in_flight = 64

        state.message = "x" * 70
        redraw(state)
        await once(lambda: len(connection.client.pacing.unread) == 1, 5.0, "the frame never went")
        # Nothing was out, so it went whatever its size.
        assert connection.client.pacing.waits_for_terminal


#: The round trip to Carl's Mac, as the audio link measures it.
ROUND_TRIP = 0.05


class Delay(NamedTuple):
    """
    A key's wait for its frame: in fair time, on the wall, how much of it
    the collector paused, and the CPU this process spent. CPU near the
    wall is pymux working; far under it, with no fair stall, is a wait
    the run queue does not see, such as memory reclaim.
    """

    fair: float
    wall: float
    collected: float
    cpu: float

    def __repr__(self) -> str:
        return "%.3f fair, %.3f wall, %.3f collecting, %.3f cpu" % self


#: F5 and F6, and the word each shows.
KEYS = (("\x1b[15~", "alpha"), ("\x1b[17~", "kilo"))


async def test_a_key_draws_within_a_round_trip_while_a_pane_animates(request):
    """
    Something on the screen animates, so the client always has a frame
    in flight and waits for its terminal. A key's frame goes once the
    terminal answered, and not on a timer after; and the frames skipped
    meanwhile ask for no others. Measured before the fix: keys up to
    104 ms, and 1,307 redraws a second for 36 frames, each a walk of the
    layout. Lillecarl/pyterm#826.
    """
    packets: list = []
    # Each frame and each key at a fair time: the server runs in this
    # process, so on a loaded machine a key waits for a CPU as well as
    # for its frame, and only the second is pymux's. Lillecarl/pyterm#981.
    fair = Stopwatch()
    # Each frame: its fair time, its wall time, this process's CPU time,
    # and what it drew.
    arrived: list[tuple[float, float, float, str]] = []

    def read(packet) -> None:
        packets.append(packet)
        if packet.get("cmd") == "out" and packet.get("data"):
            arrived.append((fair.elapsed(), time.monotonic(), time.process_time(), packet["data"]))

    # The collector's pauses, as (began, ended): one in a key's window
    # is time this process ran, so the fair stopwatch keeps it in.
    collected: list[list[float]] = []

    def collecting(phase: str, _info: dict) -> None:
        if phase == "start":
            collected.append([time.monotonic(), math.inf])
        elif collected:
            collected[-1][1] = time.monotonic()

    gc.callbacks.append(collecting)
    request.addfinalizer(lambda: gc.callbacks.remove(collecting))
    # What the suite's earlier tests left alive is not this test's heap:
    # in a worker that had run thousands of them, one collection held a
    # key for 0.343 s of its 0.353 s. Frozen, it is never scanned, and a
    # collection of what pymux makes here still counts. Not unfrozen
    # after: that would also thaw the heap the conftest froze.
    gc.freeze()

    async with over_connection(read_packet=read) as session:
        state = await attach_paced(session, packets)
        mux = session.pymux
        with set_app(state.app):
            # Two keys and two words, in turn: a frame is a diff against
            # the last one, so a word drawn over itself draws nothing.
            mux.handle_command("bind-key -n F5 display-message alpha")
            mux.handle_command("bind-key -n F6 display-message kilo")
        renders = state.app.render_counter

        # When the terminal answered, on the wall: for the timeline of a
        # key that took too long.
        answers: list[float] = []

        async def terminal() -> None:
            "Reads each frame, and answers its report a round trip after."
            answered = 0
            while True:
                sent = sum(data.count(STATUS_REPORT) for *_, data in arrived)
                for _ in range(sent - answered):
                    await anyio.sleep(ROUND_TRIP)
                    session.typed(state, ANSWER)
                    answers.append(time.monotonic())
                answered = sent
                await anyio.sleep(0.002)

        async def spinner() -> None:
            for turn in itertools.count():
                with set_app(state.app):
                    mux.handle_command("rename-window spin%d" % turn)
                await anyio.sleep(0.05)

        delays = []
        # Each key's press and its frame, on the wall.
        windows: list[tuple[float, float]] = []
        started, started_fair = time.monotonic(), fair.elapsed()
        drawn_before = len(arrived)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(terminal)
            tasks.start_soon(spinner)
            await anyio.sleep(0.5)
            for key in range(12):
                await anyio.sleep(0.037 * (key % 5) + 0.1)
                before = len(arrived)
                pressed, pressed_wall, pressed_cpu = fair.elapsed(), time.monotonic(), time.process_time()
                key_sequence, word = KEYS[key % 2]
                session.typed(state, key_sequence)
                await once(
                    lambda: any(word in data for *_, data in arrived[before:]),
                    5.0,
                    lambda: (
                        "key %d's frame never came: %r, %r"
                        % (len(delays), [data for *_, data in arrived[before:]][-3:], state.message)
                    ),
                )
                at, at_wall, at_cpu = next((at, wall, cpu) for at, wall, cpu, data in arrived[before:] if word in data)
                paused = sum(max(0.0, min(ended, at_wall) - max(began, pressed_wall)) for began, ended in collected)
                delays.append(Delay(at - pressed, at_wall - pressed_wall, paused, at_cpu - pressed_cpu))
                windows.append((pressed_wall, at_wall))
            tasks.cancel_scope.cancel()

        def worst_window() -> str:
            "What came and went around the slowest key: did it wait for an answer, or after one?"
            worst = max(range(len(delays)), key=lambda key: delays[key].fair)
            pressed_wall, at_wall = windows[worst]
            word = KEYS[worst % 2][1]
            events = [
                (wall, "frame%s%s" % (" report" if STATUS_REPORT in data else "", " key" if word in data else ""))
                for _, wall, _cpu, data in arrived
                if pressed_wall - 2 * ROUND_TRIP <= wall <= at_wall
            ]
            events += [(wall, "answer") for wall in answers if pressed_wall - 2 * ROUND_TRIP <= wall <= at_wall]
            return "key %d, %r: %s" % (
                worst,
                delays[worst],
                ", ".join("%+.3f %s" % (wall - pressed_wall, what) for wall, what in sorted(events)),
            )

        # A ceiling counts over the wall's seconds, which load only makes
        # easier; a floor over the fair ones, which load cannot lower.
        seconds = time.monotonic() - started
        redraws = (state.app.render_counter - renders) / seconds
        # A key may wait for the frame in flight, one round trip at most.
        assert max(delay.fair for delay in delays) < ROUND_TRIP * 1.6, (
            sorted(delays, reverse=True)[:3],
            worst_window(),
        )
        # Twenty spins and a key or two a second, and no spin of skips.
        assert redraws < 150, redraws
        # Five frames may be out, so the spinner's twenty a second all go:
        # one in flight held it to one a round trip. Lillecarl/pyterm#827.
        drawn = (len(arrived) - drawn_before) / (fair.elapsed() - started_fair)
        assert drawn > 15, drawn


async def test_the_answer_never_reaches_a_pane():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        pane = session.pymux.arrangement.get_active_pane()
        written: list = []
        pane.process.write_input = written.append

        session.typed(state, ANSWER)
        await once(lambda: state.client.pacing.answers_reports, 5.0, "the answer was not read")

        assert "".join(written) == ""
