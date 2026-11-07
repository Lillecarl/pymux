"""
A client whose terminal draws kitty's extra cursors gets the focused
pane's as its own, moved to where the pane is drawn, and the pane draws
no cells for them on that client. Lillecarl/pyterm#893.
"""

from __future__ import annotations

import shlex

from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from run_commands import run_as
from session import once, over_connection

from pymux.core.main import Pymux

SIZE = Size(rows=12, columns=60)
SUPPORT = "\x1b[>1;2;3;29;30;40;100;101 q"
CLEAR = "\x1b[>0;4 q"
PROGRAM = "sh -c %s" % shlex.quote("printf 'ready\\033[>1;2:2:3 q\\033[>40;5:1 q'; sleep 30")


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


async def test_the_focused_panes_cursors_go_to_kitty_where_the_pane_is():
    recorder = Recorder()
    pymux = Pymux(startup_command=PROGRAM)
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        await once(lambda: "ready" in recorder.out, 10.0, "the pane never drew")
        state.client.connection._process({Field.CMD: Packet.DETECT})
        state.client.connection._process({"cmd": "in", "data": SUPPORT})
        await once(lambda: state.client.replies.extra_cursors, 10.0, "the answer was never read")

        pane = state.session.arrangement.get_active_window().active_pane
        where = state.layout_manager.pane_write_positions[pane]
        point = "\x1b[>1;2:%d:%d q" % (where.ypos + 2, where.xpos + 3)
        await once(lambda: point in recorder.out, 10.0, lambda: "never sent: %r" % recorder.out[-300:])
        assert CLEAR in recorder.out
        assert "\x1b[>40;5:1 q" in recorder.out
        with set_app(state.app):
            assert not pane.terminal.terminal_control.draws_extra_cursors()

        # Another pane takes the focus: kitty's cursors go, and the
        # first pane draws its cursor as a cell again.
        sent = len(recorder.out)
        await run_as(pymux, state, "split-window -h %s" % shlex.quote("sleep 30"))
        await once(lambda: CLEAR in recorder.out[sent:], 10.0, "the cursors were never cleared")
        assert point not in recorder.out[sent:]


async def test_a_terminal_that_did_not_answer_gets_nothing():
    recorder = Recorder()
    async with over_connection(pymux=Pymux(startup_command=PROGRAM), read_packet=recorder) as session:
        await session.attach("one", SIZE)
        await once(lambda: "ready" in recorder.out, 10.0, "the pane never drew")
        assert "\x1b[>1;2:" not in recorder.out
        assert CLEAR not in recorder.out
