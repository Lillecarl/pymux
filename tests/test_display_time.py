"""
`display-time` takes a message down that many milliseconds after it
shows, as tmux's status_message_set does. 0 keeps it until a key is
pressed, pymux's default. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, rows_of, shows  # noqa: F401 -- a fixture


async def test_a_message_goes_after_display_time(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("set -g display-time 200")
            pymux.handle_command("display-message 'brief note'")
        await shows(session, "brief note")
        await _until(lambda: "brief note" not in "\n".join(rows_of(session)))
        assert client.message is None


async def test_a_message_stays_while_display_time_is_zero(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("display-message 'lasting note'")
        await shows(session, "lasting note")
        await anyio.sleep(0.3)
        await client.drawn_again(5.0)
        assert "lasting note" in "\n".join(rows_of(session))


async def _until(condition, seconds: float = 10.0) -> None:
    with fair_anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)
