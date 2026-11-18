"""
`status-position top` draws the status line on the first row, and the
panes under it. Lillecarl/pyterm#1035.
"""

from __future__ import annotations

import anyio
from fairclock import anyio as fair_anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, rows_of, shows  # noqa: F401 -- a fixture


async def test_the_status_line_moves_to_the_top(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client.state
        status = "[%s]" % client.session.name
        assert rows_of(session)[-1].startswith(status)

        with set_app(client.app):
            pymux.handle_command("set -g status-position top")
        await _until(lambda: rows_of(session)[0].startswith(status))

        assert not rows_of(session)[-1].startswith(status)


async def _until(condition, seconds: float = 10.0) -> None:
    with fair_anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)
