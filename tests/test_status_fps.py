"""
`status-fps`: the frames a second this client gets, before the clock.

The rules this judges: off, the status line shows no frame rate; on, it
shows one, and the clock beside it is not cut off. Lillecarl/pymux#596.
"""

from __future__ import annotations

import re
import time

from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, rows_of, settled, shows

from pymux.main import Pymux

#: The default status-right in test mode: the clock and the date.
CLOCK = re.compile(r"\d\d:\d\d \d\d-\w\w\w-\d\d")


async def test_the_frame_rate_goes_before_the_clock():
    pymux = Pymux()
    pymux.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        await shows(session, CLOCK)
        assert "fps" not in rows_of(session)[-1]

        with set_app(pymux.connections[-1].client_state.app):
            pymux.handle_command("set-option -g status-fps on")
        await shows(session, re.compile(r"\d+\.?\d* fps " + CLOCK.pattern))
        await settled(session)
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()


async def test_a_window_pane_s_frames_show_beside_them():
    """
    A window pane's frames go out apart from the status line's, and a
    video in one showed as a few frames a second. They show as their own
    number while one draws, and not at all otherwise.
    """
    pymux = Pymux()
    pymux.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        await shows(session, CLOCK)
        connection = pymux.connections[-1]
        with set_app(connection.client_state.app):
            pymux.handle_command("set-option -g status-fps on")
            right = connection.client_state.layout_manager._get_status_right_tokens
            # A count read two seconds ago, before any read now keeps one.
            connection.rate_window_frames.read(0, time.monotonic() - 2)
            assert " wl" not in right()

            # Sixty frames a second since.
            connection.window_frames_sent = 120
            assert "· 60 wl" in right()
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()
