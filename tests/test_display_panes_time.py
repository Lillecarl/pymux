"""
`display-panes` takes the pane numbers down after `display-panes-time`,
or after `-d` milliseconds, as tmux's window-panes.c does. 0 keeps them
until a key. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture


async def test_the_numbers_go_after_display_panes_time(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("set -w display-panes-time 100")
            pymux.handle_command("display-panes")
        assert pymux.display_pane_numbers
        await _until(lambda: not pymux.display_pane_numbers)


async def test_d_zero_keeps_them_until_a_key(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("set -w display-panes-time 100")
            pymux.handle_command("display-panes -d 0")
        await anyio.sleep(0.3)
        assert pymux.display_pane_numbers


async def test_a_later_display_panes_gets_its_whole_time(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        with set_app(client.app):
            pymux.handle_command("display-panes -d 100")
            pymux.handle_command("display-panes -d 0")
        await anyio.sleep(0.3)
        assert pymux.display_pane_numbers


async def _until(condition, seconds: float = 10.0) -> None:
    with fair_anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)
