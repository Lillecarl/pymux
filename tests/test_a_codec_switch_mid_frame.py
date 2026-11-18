"""
A window pane whose `window-codec` changes while a frame of it is on
its way gets a frame in the new codec after that one, though the
window draws nothing new. The switch asks for a frame by drawing the
client, and a draw that comes while an update is out was dropped: the
window waited for a frame that never came. Lillecarl/pyterm#1048.
"""

from __future__ import annotations

import anyio
from libpymux.protocol import Codec, Field
from test_a_window_pane_comes_back import Window, _run, wait_for

from pymux.graphics.graphics import ClientGraphics
from pymux.graphics.window_frames import VideoFrame, WindowView


class SwitchingWindow(Window):
    "The same window, which can also answer its frames as VP9."

    async def video(self, _encoder: object) -> VideoFrame:
        self.serial += 1
        return self.serial, True, 1, 1, b"vp9", [(0, 0, 1, 1)]

    def in_vp9(self) -> WindowView:
        return self.view()._replace(codec=Codec.VP9.value, video=self.video, encoder=object)


async def test_a_codec_switch_while_a_frame_is_out_gets_its_own_frame():
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
        graphics.windows.codecs = frozenset({Codec.VP9.value})
        window = SwitchingWindow()

        graphics.windows.render([window.view()])
        await wait_for(lambda: window.asked == [None])
        window.release.set()
        await wait_for(lambda: len(sent) == 1)

        # A raw frame of changes is being read when the codec switches.
        graphics.windows.refresh(window.view())
        await wait_for(lambda: window.asked == [None, 1])
        graphics.windows.render([window.in_vp9()])
        window.release.set()

        await wait_for(lambda: any(part.get(Field.CODEC) == Codec.VP9 for part in sent))
        tasks.cancel_scope.cancel()
