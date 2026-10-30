"""
A slow link carries the newest screen, and never a backlog.

Over ssh the frames a server sends wait in buffers nobody here can see:
sshd's, the kernel's, the link. A phone on a weak signal then shows
what was true seconds ago, and a keystroke's frame waits behind every
frame of an animation before it. So a client whose terminal answers a
status report gets one behind each frame, and the next frame waits for
the answer. Lillecarl/pymux#259.

A terminal that never answered is never waited for, and a pane's
program never waits on any of it: pyte reads the pane whether anybody
draws or not.
"""

from __future__ import annotations

import itertools
import time

import anyio
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.server import STATUS_REPORT

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

        assert not state.connection.waits_for_terminal


async def test_after_an_answer_the_next_frame_waits_for_the_next_one():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        connection = state.connection

        # Every report answered: the terminal is known to answer.
        for _ in range(connection.reports_out):
            session.typed(state, ANSWER)
        await once(lambda: connection.answers_reports and not connection.reports_out, 5.0, "answers not read")

        state.message = "one"
        redraw(state)
        await once(lambda: connection.reports_out == 1, 5.0, "the frame never went")
        sent = len(frames_of(packets))

        # Unanswered: more changes draw nothing.
        state.message = "two"
        redraw(state)
        state.message = "three"
        redraw(state)
        await once(lambda: connection.waits_for_terminal, 5.0, "the client was not held")

        # The answer draws the newest screen, once: a frame that went
        # while it was held would make the count overshoot.
        session.typed(state, ANSWER)
        await once(lambda: len(frames_of(packets)) > sent, 5.0, "the answer drew nothing")
        await once(lambda: connection.waits_for_terminal, 5.0, "the new frame was not held for")
        assert len(frames_of(packets)) == sent + 1
        assert "three" in frames_of(packets)[-1]


#: The round trip to Carl's Mac, as the audio link measures it.
ROUND_TRIP = 0.05
#: F5 and F6, and the word each shows.
KEYS = (("\x1b[15~", "alpha"), ("\x1b[17~", "kilo"))


async def test_a_key_draws_within_a_round_trip_while_a_pane_animates():
    """
    Something on the screen animates, so the client always has a frame
    in flight and waits for its terminal. A key's frame goes once the
    terminal answered, and not on a timer after; and the frames skipped
    meanwhile ask for no others. Measured before the fix: keys up to
    104 ms, and 1,307 redraws a second for 36 frames, each a walk of the
    layout. Lillecarl/pymux#824.
    """
    packets: list = []
    arrived: list[tuple[float, str]] = []

    def read(packet) -> None:
        packets.append(packet)
        if packet.get("cmd") == "out" and packet.get("data"):
            arrived.append((time.monotonic(), packet["data"]))

    async with over_connection(read_packet=read) as session:
        state = await attach_paced(session, packets)
        mux = session.pymux
        with set_app(state.app):
            # Two keys and two words, in turn: a frame is a diff against
            # the last one, so a word drawn over itself draws nothing.
            mux.handle_command("bind-key -n F5 display-message alpha")
            mux.handle_command("bind-key -n F6 display-message kilo")
        renders = state.app.render_counter

        async def terminal() -> None:
            "Reads each frame, and answers its report a round trip after."
            answered = 0
            while True:
                sent = sum(data.count(STATUS_REPORT) for _, data in arrived)
                for _ in range(sent - answered):
                    await anyio.sleep(ROUND_TRIP)
                    session.typed(state, ANSWER)
                answered = sent
                await anyio.sleep(0.002)

        async def spinner() -> None:
            for turn in itertools.count():
                with set_app(state.app):
                    mux.handle_command("rename-window spin%d" % turn)
                await anyio.sleep(0.05)

        delays = []
        started = time.monotonic()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(terminal)
            tasks.start_soon(spinner)
            await anyio.sleep(0.5)
            for key in range(12):
                await anyio.sleep(0.037 * (key % 5) + 0.1)
                before = len(arrived)
                pressed = time.monotonic()
                key_sequence, word = KEYS[key % 2]
                session.typed(state, key_sequence)
                await once(
                    lambda: any(word in data for _, data in arrived[before:]),
                    5.0,
                    lambda: (
                        "key %d's frame never came: %r, %r"
                        % (len(delays), [data for _, data in arrived[before:]][-3:], state.message)
                    ),
                )
                delays.append(next(at for at, data in arrived[before:] if word in data) - pressed)
            tasks.cancel_scope.cancel()
        seconds = time.monotonic() - started
        redraws = (state.app.render_counter - renders) / seconds
        # A key may wait for the frame in flight, one round trip at most.
        assert max(delays) < ROUND_TRIP * 1.6, sorted(delays)
        # Twenty spins and a key or two a second, and no spin of skips.
        assert redraws < 150, redraws


async def test_the_answer_never_reaches_a_pane():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state = await attach_paced(session, packets)
        pane = session.pymux.arrangement.get_active_pane()
        written: list = []
        pane.process.write_input = written.append

        session.typed(state, ANSWER)
        await once(lambda: state.connection.answers_reports, 5.0, "the answer was not read")

        assert "".join(written) == ""
