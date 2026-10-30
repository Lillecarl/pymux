"""
A client of a session that holds no window still draws.

A session has no window before the first one is made and after the
last one goes, and a client can be attached through either moment.
The status line formatted `status-left` against the active window and
raised "No active window to format for", so the client's last frame
crashed the application as it detached. Lillecarl/pymux#581.
"""

from __future__ import annotations

import io

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from session import Connection

from pymux.core.main import Pymux

ROWS, COLUMNS = 24, 80


@pytest.fixture
async def client():
    "A client of a session that has never had a window."
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running():
        output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=ROWS, columns=COLUMNS))
        with create_pipe_input() as pipe:
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=Connection(),
            )
            assert not state.session.arrangement.windows
            yield state


async def test_its_last_frame_draws(client):
    with set_app(client.app):
        client.app.renderer.render(client.app, client.app.layout, is_done=True)


@pytest.mark.parametrize("status", [True, False])
async def test_what_time_moves_has_an_answer(client, status):
    "With the status line off, the panes in view are what it reads."
    client.layout_manager.pymux.session_defaults.status = status
    with set_app(client.app):
        client.layout_manager.what_time_moves()
