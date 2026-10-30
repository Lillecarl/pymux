"""
A window pane shown again after its window was switched away draws
again. Lillecarl/pymux#753.

Leaving view deletes the client's image of the pane. An update encoded
before that, against the image as it was, must not land on the empty
one: a frame of changes onto an image the terminal no longer has draws
nothing, and every frame after it is changes too. Carl saw it as a
Firefox video that stayed blank after switching windows and back.
"""

from __future__ import annotations

import anyio
from libpymux.protocol import Field

from pymux.graphics.graphics import ClientGraphics
from pymux.graphics.window_frames import FrameRects, WindowView

#: A pixel, in the shape `frame_rects` answers rectangles.
PIXEL = (0, 0, 1, 1, b"\x00\x00\x00")


class Window:
    "A window pane whose next frame waits until the test lets it go."

    def __init__(self) -> None:
        self.serial = 0
        self.asked: list[int | None] = []
        self.release = anyio.Event()

    async def rects(self, since: int | None) -> FrameRects:
        self.asked.append(since)
        await self.release.wait()
        self.release = anyio.Event()
        self.serial += 1
        return self.serial, since is None, 1, 1, [PIXEL]

    def view(self) -> WindowView:
        async def update(_outer_id: int, _since: int | None):
            raise AssertionError("a client that takes frames as packets is never sent kitty commands")

        return WindowView(
            pane_id=1, x=0, y=0, width=1, height=1, update=update, shown=lambda _sent: None, rects=self.rects
        )


async def test_a_window_shown_again_mid_frame_gets_a_whole_frame():
    sent: list[dict] = []

    async def send_frame(parts: list[dict]) -> None:
        sent.extend(parts)

    async with anyio.create_task_group() as tasks:
        graphics = ClientGraphics(
            write_raw=lambda _data: None,
            flush=lambda: None,
            spawn=lambda coroutine: tasks.start_soon(_run, coroutine),
        )
        graphics.kitty_supported = True
        graphics.windows.send_frame = send_frame
        window = Window()

        # The first frame, whole.
        graphics.windows.render([window.view()])
        await wait_for(lambda: window.asked == [None])
        window.release.set()
        await wait_for(lambda: len(sent) == 1)

        # The window draws, and its frame of changes is being read when
        # the person switches away and straight back.
        graphics.windows.refresh(window.view())
        await wait_for(lambda: window.asked == [None, 1])
        graphics.windows.render([])
        graphics.windows.render([window.view()])
        window.release.set()

        # What reaches the client after it deleted its image starts whole.
        await wait_for(lambda: len(window.asked) == 3)
        window.release.set()
        await wait_for(lambda: len(sent) == 2)
        assert sent[1][Field.WHOLE]
        tasks.cancel_scope.cancel()


async def _run(coroutine) -> None:
    await coroutine


async def wait_for(condition, seconds: float = 2.0) -> None:
    with anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0)
