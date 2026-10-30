"""
A pane that scrolls goes out as a scroll region across the whole width,
and kitty moves an image of the pane beside it with the region. The
image is put back in the same frame's graphics. Lillecarl/pymux#821.
"""

from __future__ import annotations

import base64
import re

from run_commands import run_as
from session import DEFAULT_SIZE, once, over_connection

#: A 2x2 red image over four columns and two rows, with no cursor move.
RED = "\x1b_Ga=T,f=24,s=2,v=2,c=4,r=2,C=1;" + base64.b64encode(bytes((255, 0, 0)) * 4).decode() + "\x1b\\"

#: A scroll inside a region: the margins, then SU or SD.
REGION_SCROLL = re.compile(r"\x1b\[\d+;\d+r\x1b\[\d*[ST]")


async def test_an_image_beside_a_scrolling_pane_is_put_back():
    sent: list[str] = []
    answers: list[str] = []

    def read(packet) -> None:
        if packet.get("cmd") == "out":
            data = packet.get("data", "")
            sent.append(data)
            # The server waits for the terminal to answer the status
            # report behind a frame before it sends the next one.
            answers.extend("\x1b[0n" for _ in range(data.count("\x1b[5n")))

    async with over_connection(read_packet=read) as session:
        state, _size = await session.attach("one", DEFAULT_SIZE)

        async def sees(question, complaint: str) -> None:
            "Wait as `once` does, answering the terminal's status reports meanwhile."

            def asked() -> bool:
                while answers:
                    session.typed(state, answers.pop())
                return bool(question())

            await once(asked, 10.0, complaint)

        graphics = state.connection.graphics
        graphics.kitty_supported = True
        graphics.detected = True
        pymux = session.pymux
        # A program that writes nothing, so the rows beside the image stay blank.
        await run_as(pymux, state, "split-window -h 'sleep 60'")
        left, right = pymux.arrangement.get_active_window().panes
        right.terminal.terminal_control.stream.feed("\x1b[?25l\x1b[2;1H" + RED)
        # Fed past the pty, which is what asks for a frame.
        pymux.invalidate()
        await sees(lambda: "\x1b_Ga=p," in "".join(sent), "the image was never put")

        # A full-screen program on the left scrolls its own region, as
        # Claude Code streaming does: the renderer sends that on as a scroll.
        stream = left.terminal.terminal_control.stream
        stream.feed("\x1b[?1049h" + "".join("\x1b[%d;1Hline %d" % (row + 1, row) for row in range(20)))
        pymux.invalidate()
        await sees(lambda: "line 19" in "".join(sent), "the left pane never filled")
        # The first scroll a window sees only marks where its reports start.
        mark = len("".join(sent))
        stream.feed("\x1b[1;20r\x1b[S\x1b[r\x1b[20;1Hline 20")
        pymux.invalidate()
        await sees(lambda: len("".join(sent)) > mark, "the first scroll was never drawn")
        mark = len("".join(sent))
        stream.feed("\x1b[1;20r\x1b[S\x1b[r\x1b[20;1Hline 21")
        pymux.invalidate()
        await sees(lambda: REGION_SCROLL.search("".join(sent), mark), "the left pane never scrolled by a region")

        def put_after_the_scroll() -> bool:
            text = "".join(sent)
            scroll = REGION_SCROLL.search(text, mark)
            return scroll is not None and "\x1b_Ga=p," in text[scroll.end() :]

        await sees(put_after_the_scroll, "the image was not put back after the region scrolled")
