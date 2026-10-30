"""
A text emoji with U+FE0F, drawn for the terminal in front of the client.

A pane holds `✳️` one column wide, as six emulators of seven do. kitty
draws it two wide and over the next cell, so a client whose terminal
answers the probe that way is sent the character without the selector.
Lillecarl/pymux#539, and deviation 27 in `ptterm/tests/DEVIATIONS.md`.
"""

from __future__ import annotations

from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.connection.replies import EMOJI_WIDTH_PROBE
from pymux.core.main import Pymux

SIZE = Size(rows=10, columns=40)
EMOJI = "✳️"


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


async def _answered(answer: str, drawn: str) -> tuple[str, bool]:
    """
    Answer the probe with `answer`, and wait for the pane's line to go
    out as `drawn` after it. What went out after the answer, and the
    reading the connection took.
    """
    recorder = Recorder()
    command = "sh -c 'printf \"%sX\\n\"; sleep 30'" % EMOJI
    async with over_connection(pymux=Pymux(startup_command=command), read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        await once(lambda: "X" in recorder.out, 10.0, "the pane never drew")
        state.connection._process({Field.CMD: Packet.DETECT})
        await once(lambda: EMOJI_WIDTH_PROBE in recorder.out, 10.0, "the terminal was never asked")
        before = len(recorder.out)
        state.connection._process({"cmd": "in", "data": answer})
        await once(lambda: drawn in recorder.out[before:], 10.0, lambda: "never drawn: %r" % recorder.out[before:])
        return recorder.out[before:], state.connection.replies.widens_text_emoji


async def test_a_terminal_that_widens_it_is_sent_the_character_alone():
    # The renderer puts the cursor back after the character itself
    # ("\r\x1b[C"), so what follows it is not written beside it.
    after, widens = await _answered("\x1b[2;3R", "✳")
    assert widens
    assert "️" not in after


async def test_a_terminal_that_keeps_it_narrow_changes_nothing():
    # No repaint follows, so the line drawn before the answer stands,
    # selector and all; the next frame of the pane keeps it too.
    recorder = Recorder()
    command = 'sh -c \'printf "%sX\\n"; sleep 0.5; printf "%sY\\n"; sleep 30\'' % (EMOJI, EMOJI)
    async with over_connection(pymux=Pymux(startup_command=command), read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        state.connection._process({Field.CMD: Packet.DETECT})
        await once(lambda: EMOJI_WIDTH_PROBE in recorder.out, 10.0, "the terminal was never asked")
        before = len(recorder.out)
        state.connection._process({"cmd": "in", "data": "\x1b[2;2R"})
        await once(lambda: EMOJI in recorder.out[before:], 10.0, lambda: "never drawn: %r" % recorder.out[before:])
        assert not state.connection.replies.widens_text_emoji
