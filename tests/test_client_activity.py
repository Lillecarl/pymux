"""
`#{client_activity}`: when a person last pressed a key or used the mouse
at a client. tmux's own variable. pymux-mcp reads it to tell a browser
window somebody uses from one nobody does. Lillecarl/pymux#822.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.answer import Answer

SIZE = Size(rows=24, columns=80)


async def _activity(session) -> int:
    session.pymux.answer = Answer()
    try:
        session.pymux.handle_command("list-clients -F '#{client_activity}'")
        (line,) = session.pymux.answer.output
        return int(line)
    finally:
        session.pymux.answer = None


async def test_a_key_moves_it_and_a_terminal_reply_does_not():
    async with over_connection() as session:
        state, _size = await session.attach("one", SIZE)
        connection = state.connection
        connection._pipeinput.activity = 0.0
        # The answer to a status report, which every frame asks for.
        session.typed(state, "\x1b[0n")
        await once(lambda: True, 1.0, "")
        assert await _activity(session) == 0
        session.typed(state, "x")
        await once(lambda: connection.activity > 0, 5.0, "a key never moved client_activity")
        assert await _activity(session) == int(connection.activity)
