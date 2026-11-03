"""
kitty's drag and drop protocol (OSC 72) through pymux: pymux registers
with a terminal that answers the query, a drop over a pane whose program
registered goes to that pane at its own cells and under its `i`, a drop
over any other pane is pymux's to paste, and a drag a pane offers goes
between that pane and the terminal. Lillecarl/pymux#892.
"""

from __future__ import annotations

import base64
import re
import sys

import pytest
from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from run_commands import run_as
from session import once, over_connection

from pymux.core.drag_and_drop import server_machine_id
from pymux.core.drop_files import decoded, paths_of
from pymux.core.main import Pymux

SIZE = Size(rows=12, columns=60)
QUERY = "\x1b]72;t=q\x1b\\"
#: Not this machine's id, as kitty hashes it.
ELSEWHERE = "1:" + "0" * 64


def osc(param: str) -> str:
    return "\x1b]72;%s\x1b\\" % param


def b64(data: str) -> str:
    return base64.b64encode(data.encode()).decode()


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


async def sent(recorder: Recorder, text: str) -> None:
    "Wait until the terminal was sent `text`: output goes out on a task of its own."
    await once(lambda: text in recorder.out, 10.0, lambda: "never sent %r: %r" % (text, recorder.out[-300:]))


def hear(pane) -> list[str]:
    "What the pane's program is sent, from here on."
    heard: list[str] = []
    pane.terminal.screen.write_process_input = heard.append
    return heard


def feed(state, pane, text: str) -> None:
    with set_app(state.app):
        pane.terminal.terminal_control.stream.feed(text)


def say(state, text: str) -> None:
    "The terminal sends `text`."
    state.connection._process({"cmd": "in", "data": text})


async def attached_to_kitty(pymux, session, recorder: Recorder, *, split: bool = False):
    "A client whose terminal answered the query, drawn, with its panes."
    state, _ = await session.attach("one", SIZE)
    if split:
        await run_as(pymux, state, "split-window -h")
    state.connection._process({Field.CMD: Packet.DETECT})
    await once(lambda: QUERY in recorder.out, 10.0, "the terminal was never asked")
    say(state, QUERY)
    state.app.invalidate()
    await once(
        lambda: len(state.layout_manager.pane_write_positions) == (2 if split else 1),
        10.0,
        "the panes were never drawn",
    )
    positions = sorted(state.layout_manager.pane_write_positions.items(), key=lambda item: item[1].xpos)
    return state, [pane for pane, _ in positions], [at for _, at in positions]


async def test_pymux_registers_with_a_terminal_that_answers():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        await attached_to_kitty(pymux, session, recorder)
        await sent(recorder, osc("t=a;text/uri-list text/plain;charset=utf-8 text/plain"))


async def test_a_pane_hears_its_query_answered():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (pane,), _ = await attached_to_kitty(pymux, session, recorder)
        heard = hear(pane)
        feed(state, pane, osc("t=q:i=5"))
        assert heard == [osc("t=q:i=5")]


async def test_a_drop_reaches_the_registered_pane_at_its_own_cells():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (left, right), (_, at) = await attached_to_kitty(pymux, session, recorder, split=True)
        heard = hear(right)
        feed(state, right, osc("t=a:i=7;text/x-mine"))
        await sent(recorder, "text/x-mine")

        x, y = at.xpos + 2, at.ypos + 1
        say(state, osc("t=m:x=%d:y=%d:X=0:Y=0:o=3;text/plain text/x-mine" % (x, y)))
        assert heard == [osc("t=m:x=2:y=1:X=0:Y=0:o=3:i=7;text/plain text/x-mine")]
        heard.clear()

        # Its answer goes up without its i.
        feed(state, right, osc("t=m:o=1:i=7;text/x-mine"))
        await sent(recorder, osc("t=m:x=0:y=0:X=0:Y=0:o=1;text/x-mine"))

        say(state, osc("t=M:x=%d:y=%d:X=0:Y=0:o=3;text/plain text/x-mine" % (x, y)))
        assert heard == [osc("t=M:x=2:y=1:X=0:Y=0:o=3:i=7;text/plain text/x-mine")]
        heard.clear()

        feed(state, right, osc("t=r:x=2:i=7"))
        await sent(recorder, osc("t=r:x=2"))
        say(state, osc("t=r:x=2:m=1;aGk=") + osc("t=r:x=2"))
        assert heard == [osc("t=r:x=2:i=7:m=1;aGk="), osc("t=r:x=2:i=7")]

        feed(state, right, osc("t=r:o=1:i=7"))
        await sent(recorder, osc("t=r:o=1"))


async def test_moving_to_another_pane_tells_the_first_it_left():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (left, right), (at_left, at_right) = await attached_to_kitty(pymux, session, recorder, split=True)
        heard = hear(right)
        feed(state, right, osc("t=a"))
        say(state, osc("t=m:x=%d:y=%d:X=0:Y=0:o=1;text/plain" % (at_right.xpos, at_right.ypos)))
        assert len(heard) == 1
        heard.clear()
        say(state, osc("t=m:x=%d:y=%d:X=0:Y=0:o=1" % (at_left.xpos, at_left.ypos)))
        assert heard == [osc("t=m:x=-1:y=-1:X=0:Y=0:o=0")]
        # pymux takes it for the left pane, which did not register.
        await sent(recorder, osc("t=m:x=0:y=0:X=0:Y=0:o=0") + osc("t=m:x=0:y=0:X=0:Y=0:o=1;text/plain"))


async def test_a_pane_that_did_not_register_gets_the_paths_pasted():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (pane,), (at,) = await attached_to_kitty(pymux, session, recorder)
        typed: list[str] = []
        pane.terminal.terminal_control.process.write_input = typed.append
        here = "x=%d:y=%d" % (at.xpos + 1, at.ypos + 1)

        say(state, osc("t=m:%s:X=0:Y=0:o=3;text/plain text/uri-list" % here))
        await sent(recorder, osc("t=m:x=0:y=0:X=0:Y=0:o=1;text/uri-list text/plain"))
        say(state, osc("t=M:%s:X=0:Y=0:o=3;text/plain text/uri-list" % here))
        await sent(recorder, osc("t=r:x=2"))

        uris = b64("file:///tmp/a%20b.txt\r\nfile:///tmp/c\r\n")
        say(state, osc("t=r:x=2:m=1;" + uris[:8]) + osc("t=r:x=2:m=0;" + uris[8:]) + osc("t=r:x=2"))
        assert typed == ["'/tmp/a b.txt' /tmp/c"]
        await sent(recorder, osc("t=r:o=1"))


async def test_a_request_with_no_drop_is_refused():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (pane,), _ = await attached_to_kitty(pymux, session, recorder)
        heard = hear(pane)
        feed(state, pane, osc("t=a:i=2") + osc("t=r:x=1:i=2"))
        assert heard == [osc("t=R:x=1:i=2;EPERM:no drop over this pane")]


async def test_a_drag_goes_between_its_pane_and_the_terminal():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    # Not the build machine's: a Mac has one, the Linux sandbox none.
    pymux.drag_and_drop.machine_id = ELSEWHERE
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (pane,), (at,) = await attached_to_kitty(pymux, session, recorder)
        heard = hear(pane)
        feed(state, pane, osc("t=o:x=1:i=3"))
        await sent(recorder, osc("t=o:x=1;" + ELSEWHERE))

        say(state, osc("t=o:x=%d:y=%d:X=40:Y=40" % (at.xpos + 4, at.ypos + 2)))
        assert heard == [osc("t=o:x=4:y=2:X=40:Y=40:i=3")]
        heard.clear()

        feed(state, pane, osc("t=o:o=1:i=3;text/plain") + osc("t=p:x=0:i=3;aGk=") + osc("t=P:x=-1:i=3"))
        await sent(recorder, osc("t=o:o=1;text/plain") + osc("t=p;aGk=") + osc("t=P:x=-1"))

        say(state, osc("t=E;OK") + osc("t=e:x=4"))
        assert heard == [osc("t=E:i=3;OK"), osc("t=e:x=4:i=3")]

        # The drag is over: a later offer is refused.
        heard.clear()
        feed(state, pane, osc("t=o:o=1:i=3;text/plain"))
        assert heard == [osc("t=E:i=3;EPERM")]


async def test_a_program_that_names_no_machine_runs_on_pymuxs():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    # Not "": the sandbox may have no /etc/machine-id.
    pymux.drag_and_drop.machine_id = ELSEWHERE
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, (pane,), (at,) = await attached_to_kitty(pymux, session, recorder)
        here = "x=%d:y=%d" % (at.xpos + 1, at.ypos + 1)
        feed(state, pane, osc("t=a:i=7;text/x-mine") + osc("t=o:x=1:i=3"))
        await sent(recorder, osc("t=o:x=1;" + ELSEWHERE))

        say(state, osc("t=m:%s:X=0:Y=0:o=1;text/x-mine" % here))
        say(state, osc("t=o:%s:X=0:Y=0" % here))
        feed(state, pane, osc("t=m:o=1:i=7;text/x-mine"))
        await sent(recorder, osc("t=m:x=0:y=0:X=0:Y=0:o=1;text/x-mine"))
        assert osc("t=a:x=1") not in recorder.out
        assert osc("t=o:x=1") not in recorder.out


async def test_a_terminal_that_never_answers_gets_no_registration():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        state.connection._process({Field.CMD: Packet.DETECT})
        await once(lambda: QUERY in recorder.out, 10.0, "the terminal was never asked")
        await run_as(pymux, state, "display-message -p done")
        assert "\x1b]72;t=a" not in recorder.out


@pytest.mark.skipif(sys.platform != "darwin", reason="Linux reads /etc/machine-id")
def test_a_mac_has_a_machine_id():
    "Lillecarl/pymux#905."
    assert re.fullmatch("1:[0-9a-f]{64}", server_machine_id())


def test_base64_chunks_decode_with_or_without_padding():
    assert decoded(["aGVsbG8gd29y", "bGQ"]) == b"hello world"
    assert decoded(["aGk=", "aGk="]) == b"hihi"
    assert decoded(["aG", "k="]) == b"hi"


def test_a_uri_list_gives_paths():
    assert paths_of("# a comment\r\nfile:///a%20b\r\nhttps://x.org/\r\n\r\nfile://host/c") == [
        "/a b",
        "https://x.org/",
        "/c",
    ]
