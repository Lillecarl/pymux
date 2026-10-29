"""
A grapheme cluster fitted to a terminal wcwidth has no table for.

The terminal answers `CLUSTER_WIDTH_PROBE` with where the cursor went
after each cluster, and the frames after are fitted to the answers.
Lillecarl/pymux#801.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.clusters import CLUSTER_WIDTH_PROBE, Drawn
from pymux.main import Pymux

SIZE = Size(rows=10, columns=40)
FLAG = "\U0001f1f8\U0001f1ea"


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


async def test_a_flag_the_terminal_draws_one_column_wide_is_padded():
    recorder = Recorder()
    command = "sh -c 'printf \"%sX\n\"; sleep 30'" % FLAG
    async with over_connection(pymux=Pymux(startup_command=command), read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        await once(lambda: "X" in recorder.out, 10.0, "the pane never drew")
        state.connection._process({"cmd": "kitty-detect"})
        await once(lambda: CLUSTER_WIDTH_PROBE in recorder.out, 10.0, "the terminal was never asked")
        before = len(recorder.out)
        state.connection._process({"cmd": "in", "data": "\x1b[4;2R"})
        # The renderer puts the cursor back after the cell itself, so
        # the blank is all that tells.
        await once(lambda: FLAG + " " in recorder.out[before:], 10.0, lambda: "never drawn: %r" % recorder.out[before:])
        assert state.connection.clusters_drawn == Drawn(flag=1)


async def test_a_terminal_too_short_for_the_probe_is_not_asked():
    recorder = Recorder()
    async with over_connection(pymux=Pymux(startup_command="sleep 30"), read_packet=recorder) as session:
        state, _ = await session.attach("one", Size(rows=4, columns=40))
        state.connection._process({"cmd": "kitty-detect"})
        await once(lambda: "\x1b]99;" in recorder.out, 10.0, "the survey never went out")
        assert CLUSTER_WIDTH_PROBE not in recorder.out
