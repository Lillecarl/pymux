"""
Clients with different cells: the panes take the cell of the client a
person used last, so the terminal in front of them draws a window pane
pixel for pixel. Lillecarl/pymux#753.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from session import over_connection

SIZE = Size(rows=24, columns=80)


def _knows(state, cell) -> None:
    graphics = state.connection.graphics
    graphics.cell_width, graphics.cell_height = cell
    graphics.cell_size_known = True


async def test_the_client_used_last_speaks_for_the_panes():
    async with over_connection() as session:
        first, _ = await session.attach("first", SIZE)
        second, _ = await session.attach("second", SIZE)
        _knows(first, (7, 16))
        _knows(second, (10, 18))
        terminals = session.pymux.terminals

        session.pymux.client_was_used(first)
        assert terminals.cell_size() == (7, 16)
        session.pymux.client_was_used(second)
        assert terminals.cell_size() == (10, 18)


async def test_a_session_takes_the_cell_of_a_client_watching_it():
    async with over_connection() as session:
        first, _ = await session.attach("first", SIZE)
        second, _ = await session.attach("second", SIZE)
        _knows(first, (7, 16))
        _knows(second, (10, 18))
        session.pymux.handle_command("new-session -d -s other")
        other = next(s for s in session.pymux.sessions if s.name == "other")
        second.session = other
        session.pymux.client_was_used(second)

        assert session.pymux.terminals.cell_size(first.session) == (7, 16)
        assert session.pymux.terminals.cell_size(other) == (10, 18)
