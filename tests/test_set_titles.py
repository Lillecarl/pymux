"""
`set-titles` gives the terminal of each client `set-titles-string` as
its title, and the title follows what the format reads, as tmux's
server_client_set_title does. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture


async def test_the_terminal_title_follows_the_window_name(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("set -g set-titles-string 'pymux #W'")
            pymux.handle_command("rename-window first")
            pymux.handle_command("set -g set-titles on")
        await _until(lambda: session.screen.titles.window == "pymux first")

        with set_app(client.app):
            pymux.handle_command("rename-window second")
        await _until(lambda: session.screen.titles.window == "pymux second")


async def _until(condition, seconds: float = 10.0) -> None:
    with fair_anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)
