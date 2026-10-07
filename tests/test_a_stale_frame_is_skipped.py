"""
A frame asked for before input is skipped, and drawn after it.

A redraw is scheduled, and input arrives before it runs: the frame
it would draw predates the input, so the renderer skips it and asks
for another one instead, which the input's own handling brings.
Equality is the steady state -- every scheduled redraw records the
tick, so a frame whose input has been handled always draws.
Lillecarl/pymux#524.
"""

from __future__ import annotations

import json

from libpymux.protocol import Field, Packet
from profile_frame import server

from pymux.server import ServerConnection


class _Pipe:
    "What the connection hands input to, recorded."

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, data: str) -> None:
        self.sent.append(data)


async def test_a_stale_frame_is_skipped_and_asked_for_again() -> None:
    async with server(1) as (pymux, state):
        # The renderer draws through the binding on the application.
        assert state.app.should_skip_render == state.should_skip_render
        # Steady state draws.
        assert not state.should_skip_render()
        # Input newer than the last schedule skips one frame...
        state.input_tick += 1
        assert state.should_skip_render()
        # ...and the schedule it brings draws again.
        pymux.client_asked_for_frame(state.app)
        assert not state.should_skip_render()
        assert state.schedule_tick == state.input_tick


async def test_input_from_the_client_moves_the_tick() -> None:
    async with server(1) as (pymux, state):
        newcomer = _Pipe()
        connection = ServerConnection.__new__(ServerConnection)
        connection._pipeinput = newcomer
        connection.client_state = state
        connection._process(json.dumps({Field.CMD: Packet.IN, Field.DATA: "x"}))
        assert newcomer.sent == ["x"]
        assert state.should_skip_render()
