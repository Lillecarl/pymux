"""
A pane's text at another size, kitty's OSC 66, reaches a terminal that
draws it, and stays cells at one size on one that does not.
Lillecarl/pymux#890.
"""

from __future__ import annotations

from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from pyte.screen import Screen
from pyte.sequences.text_sizing import TextSize
from pyte.streams import Stream
from session import once, over_connection

from pymux.connection.replies import TEXT_SIZE_PROBE
from pymux.core.main import Pymux
from pymux.graphics.graphics import ClientGraphics, PaneView
from pymux.layout.rects import Rect

SIZE = Size(rows=10, columns=40)
BIG = TextSize("H", scale=2)


class Written:
    def __init__(self) -> None:
        self.out = ""
        self.repaints = 0

    def write(self, data: str) -> None:
        self.out += data

    def repaint(self) -> None:
        self.repaints += 1


def _graphics(written: Written, widths: bool = True, scales: bool = True) -> ClientGraphics:
    graphics = ClientGraphics(written.write, lambda: None, written.repaint)
    graphics.text_sizes.widths = widths
    graphics.text_sizes.scales = scales
    return graphics


def _view(screen: Screen, covered: tuple[Rect, ...] = ()) -> PaneView:
    return PaneView(1, 3, 1, screen.columns, screen.lines, 0, 0, screen.graphics, screen, covered)


def _screen(data: str) -> Screen:
    screen = Screen(5, 10, write_process_input=lambda answer: None)
    Stream(screen).feed(data)
    return screen


def test_a_block_goes_out_once_at_its_cell():
    written = Written()
    graphics = _graphics(written)
    view = _view(_screen(str(BIG)))
    graphics.text_sizes.render([view])
    assert written.out.endswith("\x1b[2;4H\x1b[0m" + str(BIG) + "\x1b8")
    before = written.out
    graphics.text_sizes.render([view])
    assert written.out == before


def test_a_block_that_goes_asks_for_a_full_repaint():
    written = Written()
    graphics = _graphics(written)
    screen = _screen(str(BIG))
    graphics.text_sizes.render([_view(screen)])
    Stream(screen).feed("\x1b[2J")
    graphics.text_sizes.render([_view(screen)])
    assert written.repaints == 1


def test_the_full_frame_brings_the_blocks_back():
    written = Written()
    graphics = _graphics(written)
    view = _view(_screen(str(BIG)))
    graphics.text_sizes.render([view])
    graphics.text_sizes.frame_starts(full=True)
    graphics.text_sizes.render([view])
    assert written.out.count(str(BIG)) == 2


def test_a_covered_block_stays_cells():
    written = Written()
    graphics = _graphics(written)
    graphics.text_sizes.render([_view(_screen(str(BIG)), covered=(Rect(4, 2, 1, 1),))])
    assert written.out == ""


def test_a_terminal_that_draws_widths_alone_gets_no_scale():
    written = Written()
    graphics = _graphics(written, scales=False)
    graphics.text_sizes.render([_view(_screen(str(BIG) + str(TextSize("ab", width=1))))])
    assert str(BIG) not in written.out
    assert str(TextSize("ab", width=1)) in written.out


class Recorder:
    def __init__(self) -> None:
        self.out = ""

    def __call__(self, packet) -> None:
        if packet.get("cmd") == "out":
            self.out += packet.get("data", "")


async def _answered(answer: str) -> str:
    "What went out after the terminal answered the probe with `answer`, once the pane drew."
    recorder = Recorder()
    command = "sh -c 'printf \"\\033]66;s=2;H\\033\\\\X\\n\"; sleep 30'"
    async with over_connection(pymux=Pymux(startup_command=command), read_packet=recorder) as session:
        state, _ = await session.attach("one", SIZE)
        await once(lambda: "X" in recorder.out, 10.0, "the pane never drew")
        state.connection._process({Field.CMD: Packet.DETECT})
        await once(lambda: TEXT_SIZE_PROBE in recorder.out, 10.0, "the terminal was never asked")
        before = len(recorder.out)
        state.connection._process({"cmd": "in", "data": answer})
        # A full frame either way, so the one that does not draw OSC 66
        # has a frame to read too.
        state.connection.request_repaint()
        await once(lambda: "X" in recorder.out[before:], 10.0, lambda: "never drawn: %r" % recorder.out[before:])
        if state.connection.graphics.text_sizes.scales:
            await once(lambda: str(BIG) in recorder.out[before:], 10.0, lambda: "no block: %r" % recorder.out[before:])
        return recorder.out[before:]


async def test_a_terminal_that_draws_osc_66_is_sent_the_block():
    assert str(BIG) in await _answered("\x1b[6;3R\x1b[7;3R")


async def test_a_terminal_that_does_not_gets_the_cells():
    after = await _answered("\x1b[6;1R\x1b[7;1R")
    assert "\x1b]66;" not in after
