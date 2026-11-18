"""
`default-command` is what a new pane runs, through `default-shell -c`,
when nothing names a command; empty starts the shell alone, as in
tmux. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture


async def run(pymux, client, line: str) -> None:
    with set_app(client.app):
        answer = pymux.handle_command(line)
        if answer is not None:
            await answer


def newest_pane_text(pymux) -> str:
    pane = pymux.arrangement.windows[-1].active_pane
    screen = pane.terminal.terminal_control.screen
    return "\n".join(line.text for line in screen.page.text_lines(0, screen.max_y))


async def shown_in_the_new_pane(pymux, wanted: str) -> None:
    with fair_anyio.fail_after(10.0):
        while wanted not in newest_pane_text(pymux):
            await anyio.sleep(0.02)


async def test_a_new_window_runs_the_default_command(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        await run(pymux, client, "set default-command 'echo from-the-default-$((6*7)); sleep 30'")
        await run(pymux, client, "new-window")
        await shown_in_the_new_pane(pymux, "from-the-default-42")


async def test_a_named_command_wins_over_it(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        await run(pymux, client, "set default-command 'echo from-the-default; sleep 30'")
        await run(pymux, client, "new-window 'echo named-$((6*7)); sleep 30'")
        await shown_in_the_new_pane(pymux, "named-42")
        assert "from-the-default" not in newest_pane_text(pymux)
