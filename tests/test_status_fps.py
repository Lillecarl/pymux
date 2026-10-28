"""
The frame rates a status line shows: `client_fps`, the frames a second
this client gets, and `client_window_fps`, a window pane's own, which go
out apart from them. There is no option for either; a template puts them
where a person wants them, per client. Lillecarl/pymux#596.
"""

from __future__ import annotations

import re
import time

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import over_connection
from test_a_session_on_a_pyte_screen import attached, shows

from pymux.main import Pymux

#: The default status-right in test mode: the clock and the date.
CLOCK = re.compile(r"\d\d:\d\d \d\d-\w\w\w-\d\d")


async def test_a_template_shows_the_frame_rates_where_it_says():
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running(), attached(pymux) as session:
        await shows(session, CLOCK)
        connection = pymux.connections[-1]
        with set_app(connection.client_state.app):
            pymux.handle_command(
                "set-option -g status-right"
                " '{{ client_fps }}{% if client_window_fps %} / {{ client_window_fps }} wl{% endif %}'"
            )
            right = connection.client_state.layout_manager.status.right
            # A count read two seconds ago, before any read now keeps one.
            connection.rate_window_frames.read(0, time.monotonic() - 2)
            assert re.fullmatch(r"\d+\.?\d* fps", right())

            # Sixty window frames a second since: a little under, as the
            # rate divides by the seconds that passed.
            connection.window_frames_sent = 120
            assert re.search(r" / (5\d|60) fps wl$", right())
        for pane in list(pymux.panes_by_id.values()):
            pane.process.kill()


#: A template that tells a phone from a desktop by its width.
BY_WIDTH = "{% if client_width|int < 100 %}phone {{ client_width }}{% else %}desk {{ client_width }}{% endif %}"


@pytest.mark.parametrize(("columns", "drawn"), [(342, "desk 342"), (60, "phone 60")])
async def test_a_template_tells_clients_apart_by_width(columns, drawn):
    "Over a real connection: the width is what the client's terminal reported."
    async with over_connection() as session:
        await session.pymux.create_window("sleep 60")
        client, _ = await session.attach("only", Size(rows=40, columns=columns))
        with set_app(client.app):
            session.pymux.handle_command("set-option -g status-right '%s'" % BY_WIDTH)
            assert client.layout_manager.status.right() == drawn
