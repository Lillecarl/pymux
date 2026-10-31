"""
A command over the socket that moves the active pane moves every client's focus.

The layout focus is what a pane's mouse handler asks: a wheel or a click
over a pane that has no focus does nothing (Lillecarl/pymux#527). A
command from a pane's CLI runs under a client of its own, so the person
who watches is focused where they were until they press a key. Their
first wheel over the new pane does nothing, and a script that splits a
window and then scrolls it never scrolls. Lillecarl/pymux#507.
"""

from __future__ import annotations

import sys

from prompt_toolkit.data_structures import Size
from session import once, over_connection

SIZE = Size(rows=24, columns=80)
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


async def test_a_split_over_the_socket_focuses_the_new_pane():
    async with over_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("watcher", SIZE)
        await pymux.panes.create_window(WAITS)
        window = pymux.arrangement.get_active_window()

        await session.command("split-window -h %s" % WAITS)
        await once(lambda: len(window.panes) == 2, 5.0, "the split never came")

        new = window.active_pane
        assert new is window.panes[1]
        await once(
            lambda: state.app.layout.has_focus(new.terminal),
            5.0,
            lambda: "the client is focused on %r, not the new pane" % (state.app.layout.current_control,),
        )


async def test_select_pane_over_the_socket_moves_the_focus():
    async with over_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("watcher", SIZE)
        await pymux.panes.create_window(WAITS)
        window = pymux.arrangement.get_active_window()
        await session.command("split-window -h %s" % WAITS)
        await once(lambda: len(window.panes) == 2, 5.0, "the split never came")
        # From a focus on the right, or the left one would pass unmoved.
        state.sync_focus()
        assert state.app.layout.has_focus(window.panes[1].terminal)

        await session.command("select-pane -L")
        left = window.panes[0]
        await once(lambda: window.active_pane is left, 5.0, "select-pane did nothing")
        await once(
            lambda: state.app.layout.has_focus(left.terminal),
            5.0,
            lambda: "the client is focused on %r, not the left pane" % (state.app.layout.current_control,),
        )
