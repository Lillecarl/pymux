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


def osc(packet: str) -> str:
    return "\x1b]5522;%s\x1b\\" % packet


async def attached_to_kitty(session, recorder: Recorder):
    "A client whose terminal said it speaks OSC 5522."
    state, _ = await session.attach("one", SIZE)
    state.connection._process({Field.CMD: Packet.DETECT})
    state.connection._process({"cmd": "in", "data": SPEAKS_5522})
    await once(lambda: state.connection.replies.kitty_clipboard, 10.0, "the answer was never read")
    return state


async def test_a_pane_read_goes_to_the_terminal_and_its_answer_comes_back():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state = await attached_to_kitty(session, recorder)
        pane = pane_of(state)
        heard = hear_the_pane(pane)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed(
                osc("type=read:id=nvim:pw=%s:name=%s;%s" % (b64("secret"), b64("nvim"), b64("text/plain")))
            )
        asked = osc("type=read:id=pymux-1:name=%s:pw=%s;%s" % (b64("nvim"), b64("secret"), b64("text/plain")))
        await once(lambda: asked in recorder.out, 10.0, lambda: "never asked: %r" % recorder.out[-300:])

        # A second read, from any pane, waits for nobody: one is in flight.
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed(osc("type=read:id=two;%s" % b64("text/plain")))
        assert heard == [osc("type=read:status=EBUSY:id=two")]
        heard.clear()

        data = "type=read:status=DATA:id=pymux-1:mime=%s;%s" % (b64("text/plain"), b64("copied"))
        state.connection._process(
            {
                "cmd": "in",
                "data": osc("type=read:status=OK:id=pymux-1") + osc(data) + osc("type=read:status=DONE:id=pymux-1"),
            }
        )
        assert heard == [
            osc("type=read:status=OK:id=nvim"),
            osc("type=read:status=DATA:id=nvim:mime=%s;%s" % (b64("text/plain"), b64("copied"))),
            osc("type=read:status=DONE:id=nvim"),
        ]


async def test_a_pane_with_paste_events_gets_them_from_kitty():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state = await attached_to_kitty(session, recorder)
        pane = pane_of(state)
        heard = hear_the_pane(pane)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed("\x1b[?5522h")
        state.app.invalidate()
        await once(lambda: "\x1b[?5522h" in recorder.out, 10.0, "the terminal was never told")

        event = [
            osc("type=read:status=OK:pw=%s" % b64("once")),
            osc("type=read:status=DATA:mime=%s:pw=%s;%s" % (b64("."), b64("once"), b64("text/plain image/png"))),
            osc("type=read:status=DONE:pw=%s" % b64("once")),
        ]
        state.connection._process({"cmd": "in", "data": "".join(event)})
        assert heard == event

        # The pane's read under that password goes to that terminal.
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed(
                osc("type=read:id=p:pw=%s:name=%s;%s" % (b64("once"), b64("Paste event"), b64("image/png")))
            )
        await once(
            lambda: "type=read:id=pymux-1:name=%s:pw=%s" % (b64("Paste event"), b64("once")) in recorder.out,
            10.0,
            "never asked",
        )

        # The program leaves the mode, and so does the terminal.
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed("\x1b[?5522l")
        state.app.invalidate()
        await once(lambda: "\x1b[?5522l" in recorder.out, 10.0, "the terminal kept the mode")


async def test_set_clipboard_off_sends_no_paste_events():
    "The pane could not read what an event lists, so the terminal keeps its bracketed paste."
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state = await attached_to_kitty(session, recorder)
        await run_as(pymux, state, "set-option set-clipboard off")
        pane = pane_of(state)
        with set_app(state.app):
            pane.terminal.terminal_control.stream.feed("\x1b[?5522hdrawn-it")
        # A pane's draw is a frame the mode would have gone out with.
        await once(lambda: "drawn-it" in recorder.out, 10.0, "the pane never drew")
        state.app.invalidate()
        await run_as(pymux, state, "display-message -p done")
        assert "\x1b[?5522h" not in recorder.out


async def test_a_paste_event_for_a_pane_without_the_mode_is_pasted_as_text():
    recorder = Recorder()
    pymux = Pymux(startup_command="sleep 30")
    async with over_connection(pymux=pymux, read_packet=recorder) as session:
        state = await attached_to_kitty(session, recorder)
        pane = pane_of(state)
        typed: list[str] = []
        pane.terminal.terminal_control.process.write_input = typed.append

        state.connection._process(
            {
                "cmd": "in",
                "data": osc("type=read:status=OK:pw=%s" % b64("once"))
                + osc("type=read:status=DATA:mime=%s;%s" % (b64("."), b64("image/png text/plain")))
                + osc("type=read:status=DONE"),
            }
        )
        asked = osc("type=read:id=pymux-1:name=%s:pw=%s;%s" % (b64("Paste event"), b64("once"), b64("text/plain")))
        await once(lambda: asked in recorder.out, 10.0, lambda: "never asked: %r" % recorder.out[-300:])

        text = osc("type=read:status=DATA:id=pymux-1:mime=%s;%s" % (b64("text/plain"), b64("pasted")))
        state.connection._process({"cmd": "in", "data": osc("type=read:status=OK:id=pymux-1") + text})
        assert typed == []
        state.connection._process({"cmd": "in", "data": osc("type=read:status=DONE:id=pymux-1")})
        assert typed == ["pasted"]
