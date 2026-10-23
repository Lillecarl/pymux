"""
The frame rates a status line shows: `client_fps`, the frames a second
this client gets, and `client_window_fps`, a window pane's own, which go
out apart from them. There is no option for either; a template puts them
where a person wants them, per client. Lillecarl/pymux#596.
"""

from __future__ import annotations

import re
import time

from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, shows

from pymux.main import Pymux

#: The default status-right in test mode: the clock and the date.
CLOCK = re.compile(r"\d\d:\d\d \d\d-\w\w\w-\d\d")


async def test_a_template_shows_the_frame_rates_where_it_says():
    pymux = Pymux()
    pymux.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        await shows(session, CLOCK)
        connection = pymux.connections[-1]
        with set_app(connection.client_state.app):
            pymux.handle_command(
                "set-option -g status-right"
                " '{{ client_fps }}{% if client_window_fps %} / {{ client_window_fps }} wl{% endif %}'"
            )
            right = connection.client_state.layout_manager._get_status_right_tokens
            # A count read two seconds ago, before any read now keeps one.
            connection.rate_window_frames.read(0, time.monotonic() - 2)
            assert re.fullmatch(r"\d+\.?\d* fps", right())

            # Sixty window frames a second since.
            connection.window_frames_sent = 120
            assert right().endswith(" / 60 fps wl")
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()
