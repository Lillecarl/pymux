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
