"""
Copy mode belongs to the client that opens it.

Two people can watch one pane. One of them reading its history must not
stop the program for the other, and must not move the other's view or
take the other's keys. Each client is an application of its own, and
`ptterm` keeps a copy mode for each. Lillecarl/pymux#470.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import once, over_connection

SIZE = Size(rows=24, columns=80)

#: A program that answers: what is typed comes back on the screen.
ECHOES = "cat"


def _the_pane(pymux):
    return pymux.arrangement.get_active_window().active_pane


def _shown(pane) -> str:
    return "\n".join("".join(cell.char for cell in row.values()) for row in pane.screen.page.data_buffer.values())


async def test_one_client_reads_the_history_and_the_other_stays_live():
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        pane = _the_pane(pymux)

        with set_app(reading.app):
            pymux.handle_command("copy-mode")

        assert pane.is_copying_for(reading.app)
        assert not pane.is_copying_for(watching.app)
        assert not pane.process.suspended

        # The watcher types, and the program answers on the screen.
        session.typed(watching, "still here\r")
        await once(lambda: "still here" in _shown(pane), 5.0, "the program stopped for copy mode")

        # What the reader reads is the copy it opened.
        view = pane.terminal.copy_views.of(reading.app)
        assert "still here" not in view.buffer.text


async def test_the_reader_leaves_and_the_watcher_never_entered():
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.create_window(ECHOES)
        reading, _ = await session.attach("reading", SIZE)
        watching, _ = await session.attach("watching", SIZE)
        pane = _the_pane(pymux)

        with set_app(reading.app):
            pymux.handle_command("copy-mode")
        session.typed(reading, "q")

        await once(lambda: not pane.is_copying_for(reading.app), 5.0, "q never left copy mode")
        assert not pane.is_copying_for(watching.app)
