"""
kitty's clipboard protocol (OSC 5522) through pymux: a pane's write
reaches the paste buffer with every MIME type, a terminal that speaks
OSC 5522 gets it again under pymux's own id, any other terminal gets the
text as OSC 52, and the pane hears DONE with its own id.
Lillecarl/pymux#893.
"""

from __future__ import annotations

import base64

from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from run_commands import run_as
from session import once, over_connection

from pymux.core.main import Pymux
from pymux.core.rich_clipboard import RichClipboardData

SIZE = Size(rows=12, columns=60)
#: kitty's answer to DECRQM for its paste events mode: known, and reset.
SPEAKS_5522 = "\x1b[?5522;2$y"


def b64(data: str | bytes) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


PNG = bytes(range(256)) * 20
WRITE = "".join(
    "\x1b]5522;%s\x1b\\" % packet
    for packet in (
        "type=write:id=pane-1",
        "type=wdata:mime=%s;%s" % (b64("text/plain"), b64("hello")),
        "type=walias:mime=%s;%s" % (b64("text/plain"), b64("UTF8_STRING")),
        "type=wdata:mime=%s;%s" % (b64("image/png"), b64(PNG)),
        "type=wdata",
    )
)


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


def pane_of(state):
    return state.session.arrangement.get_active_window().active_pane


def hear_the_pane(pane) -> list[str]:
    "What the pane's program is sent, from here on."
    heard: list[str] = []
    pane.terminal.screen.write_process_input = heard.append
    return heard


async def test_a_write_reaches_kitty_again_and_the_pane_hears_done():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        state.connection._process({Field.CMD: Packet.DETECT})
        state.connection._process({"cmd": "in", "data": SPEAKS_5522})
        await once(lambda: state.connection.replies.kitty_clipboard, 10.0, "the answer was never read")
        await run_as(pymux, state, "set-option set-clipboard on")

        pane = pane_of(state)
        heard = hear_the_pane(pane)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed(WRITE)
        assert heard == ["\x1b]5522;type=write:status=DONE:id=pane-1\x1b\\"]

        data = pymux.clipboard.get_data()
        assert isinstance(data, RichClipboardData)
        assert data.text == "hello"
        assert data.types == {"text/plain": b"hello", "UTF8_STRING": b"hello", "image/png": PNG}

        await once(lambda: "type=wdata:id=pymux-" in recorder.out, 10.0, "the write never reached the terminal")
        assert "\x1b]5522;type=write:id=pymux-1\x1b\\" in recorder.out
        assert "\x1b]52;" not in recorder.out
        # Two chunks of the picture, each at most 4096 bytes.
        assert recorder.out.count("type=wdata:id=pymux-1:mime=%s;" % b64("image/png")) == 2
        assert "type=walias:id=pymux-1:mime=%s;%s" % (b64("text/plain"), b64("UTF8_STRING")) in recorder.out

        # kitty's answer is pymux's, and no pane hears it as keys.
        state.connection._process({"cmd": "in", "data": "\x1b]5522;type=write:status=DONE:id=pymux-1\x1b\\"})
        assert heard == ["\x1b]5522;type=write:status=DONE:id=pane-1\x1b\\"]


async def test_a_terminal_without_5522_gets_the_text_as_osc_52():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        await run_as(pymux, state, "set-option set-clipboard on")
        with set_app(state.app):
            pane_of(state).terminal.terminal_control.stream.feed(WRITE)
        await once(lambda: "\x1b]52;c;%s" % b64("hello") in recorder.out, 10.0, "the text never went out")
        # The pane's tty echoes its DONE as text; no escape goes out.
        assert "\x1b]5522" not in recorder.out


async def test_a_write_is_refused_unless_set_clipboard_is_on():
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=Recorder()) as session:
        state, _ = await session.attach("one", SIZE)
        pane = pane_of(state)
        heard = hear_the_pane(pane)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed(WRITE)
        assert heard == ["\x1b]5522;type=write:status=EPERM:id=pane-1\x1b\\"]
        assert not isinstance(pymux.clipboard.get_data(), RichClipboardData)


async def test_a_pane_answers_decrqm_for_paste_events():
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=Recorder()) as session:
        state, _ = await session.attach("one", SIZE)
        pane = pane_of(state)
        heard = hear_the_pane(pane)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed("\x1b[?5522$p")
        assert heard == ["\x1b[?5522;2$y"]
