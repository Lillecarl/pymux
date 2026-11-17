"""
The meters a status line shows, on their own clock. Lillecarl/pyterm#996.

A frame that only moves a meter or the clock is not counted, so an idle
client reads zero and then asks for nothing more; a meter sampled when
it was drawn kept a frame alive every few ticks instead.
"""

from __future__ import annotations

import re

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, shows

from pymux.core.main import Pymux


async def drawn(app) -> None:
    "Wait until the frame the application asked for went out."
    with fair_anyio.fail_after(5):
        while app.invalidated:
            await anyio.sleep(0.01)


async def test_an_idle_client_reads_zero_and_then_asks_for_nothing():
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        connection = pymux.connections[-1]
        app = connection.client.state.app
        with set_app(app):
            pymux.handle_command("set-option -g status-right 'meter {{ client_fps }} {{ client_rate_down }}'")
        await shows(session, "meter ")
        now = [1000.0]
        pymux.meters.clock = lambda: now[0]
        for _second in range(10):
            now[0] += 1
            pymux.meters.sample()
            await drawn(app)
        await shows(session, "meter 0.0 fps 0 B/s")

        for _second in range(5):
            now[0] += 1
            pymux.meters.sample()
            assert not app.invalidated, "an idle client asked for a frame"
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()


async def test_a_meter_that_moves_redraws_the_client():
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        connection = pymux.connections[-1]
        app = connection.client.state.app
        with set_app(app):
            pymux.handle_command("set-option -g status-right 'meter {{ client_fps }}'")
        now = [1000.0]
        pymux.meters.clock = lambda: now[0]
        pymux.meters.sample()
        await drawn(app)
        connection.client.pacing.frames_sent += 30
        now[0] += 1
        pymux.meters.sample()
        # Thirty, and the frames the setup drew between the samples.
        await shows(session, re.compile(r"meter \d\d fps"))
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()


async def test_content_behind_the_clocks_request_counts():
    """
    prompt_toolkit drops an invalidate while a redraw is pending, so a
    pane write behind the clock's request reaches no `on_invalidate`.
    The frame is content all the same.
    """
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        # The shell's prompt first: under load it came after the frames
        # below and was counted as their content. Lillecarl/pyterm#1000.
        await shows(session, "$")
        connection = pymux.connections[-1]
        app = connection.client.state.app
        pacing = connection.client.pacing
        with set_app(app):
            pymux.handle_command("set-option -g status-right 'link {{ link_rate_down }}'")
        now = [1000.0]
        pymux.meters.clock = lambda: now[0]
        pymux.meters.sample()
        await shows(session, "link 0 B/s")
        await drawn(app)

        # The link reading moves: a frame goes out, and no meter counts it.
        before, sent = pacing.frames_sent, connection.pipe_connection.bytes_out
        pymux.peers.bytes_down += 3000
        now[0] += 1
        pymux.meters.sample()
        await shows(session, "link 3.0 kB/s")
        assert connection.pipe_connection.bytes_out > sent
        assert pacing.frames_sent == before, "a frame only time asked for was counted"

        # A pane writes while the clock's request waits.
        app.invalidate_for_time()
        pane = pymux.arrangement.get_active_window().active_pane
        pane.terminal.terminal_control.feed_output("written behind the clock")
        await shows(session, "written behind the clock")
        assert pacing.frames_sent > before
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()
