"""
A window made where nobody watches takes its size from the clients on
the server, by `window-size`, so an agent's window is the size the
person's would be. It takes it once, when it is made: a client that
resizes later never resizes a program nobody watches.
Lillecarl/pymux#817.

`test_a_session_nobody_watches.py` holds the sizes `new-session -x -y`
names, which win over this.
"""

from __future__ import annotations

import sys

from session import once
from test_a_session_on_a_pyte_screen import COLUMNS, ROWS, attached, pymux, shows  # noqa: F401

from pymux.answer import Answer
from pymux.commands import call_command_handler

ENDS_LATER = "%s -c 'import time; time.sleep(60)'" % (sys.executable,)


async def new_session(mux, *arguments) -> None:
    mux.answer = Answer()
    try:
        answer = call_command_handler("new-session", mux, ["-d", "-s", "agent", *arguments, ENDS_LATER])
        if answer is not None:
            await answer
    finally:
        mux.answer = None


def agents_pane(mux):
    (session,) = [one for one in mux.sessions if one.name == "agent"]
    window = session.arrangement.windows[0]
    return window, window.panes[0]


async def test_a_detached_window_is_the_size_of_the_client(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await new_session(pymux)
        window, pane = agents_pane(pymux)

        # The client's terminal, less its status line.
        status_rows = pymux.clients[0].layout_manager.status.rows()
        assert pymux.planes.size(window).columns == COLUMNS
        assert pymux.planes.size(window).rows == ROWS - status_rows
        assert pane.process.sx == COLUMNS


async def test_a_size_named_wins_over_the_client(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await new_session(pymux, "-x", "100", "-y", "30")
        window, pane = agents_pane(pymux)

        assert (pane.process.sx, pane.process.sy) == (100, 30)


async def test_a_client_that_resizes_later_leaves_the_window_alone(pymux):
    "The program in it is never resized behind its back."
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await new_session(pymux)
        window, pane = agents_pane(pymux)
        before = pymux.planes.size(window)

        session.resize(ROWS + 8, COLUMNS + 20)
        connection = pymux.connections[-1]
        await once(lambda: connection.size.columns == COLUMNS + 20, 10.0, "the server never took the new size")

        assert pymux.planes.size(window) == before
        assert pane.process.sx == COLUMNS


async def test_a_window_made_after_the_resize_takes_the_new_size(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        session.resize(ROWS + 8, COLUMNS + 20)
        connection = pymux.connections[-1]
        await once(lambda: connection.size.columns == COLUMNS + 20, 10.0, "the server never took the new size")

        await new_session(pymux)
        window, _pane = agents_pane(pymux)

        assert pymux.planes.size(window).columns == COLUMNS + 20
