"""
Closing the window or the pane a client is on returns it to the newest
one it left that is still open, as tmux does (`session_last`), and not
to the next index. Lillecarl/pymux#658.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process, once

from pymux.commands import handle_command
from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)


async def _command(pymux, state, text):
    with set_app(state.app):
        answer = handle_command(pymux, text)
        if answer is not None:
            await answer


def _active_window(pymux, state):
    with set_app(state.app):
        return pymux.arrangement.get_active_window()


async def test_closing_windows_walks_back_through_the_ones_used():
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        for _ in range(3):
            await _command(pymux, state, "new-window 'sleep 30'")
        assert [int(w.index) for w in pymux.arrangement.windows] == [1, 2, 3, 4]

        # Used in the order 4, 1, 3. The next index after 3 is 4, and
        # after 1 it is 2: neither is where a person comes back to.
        for index in (4, 1, 3):
            await _command(pymux, state, "select-window -t %d" % index)

        await _command(pymux, state, "kill-window")
        await once(lambda: len(pymux.arrangement.windows) == 3, 5.0, "window 3 never closed")
        assert int(_active_window(pymux, state).index) == 1

        await _command(pymux, state, "kill-window")
        await once(lambda: len(pymux.arrangement.windows) == 2, 5.0, "window 1 never closed")
        assert int(_active_window(pymux, state).index) == 4


async def test_closing_panes_walks_back_through_the_ones_used():
    async with in_this_process(Pymux(startup_command="sleep 30")) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)
        window = _active_window(pymux, state)
        (first,) = window.panes
        await _command(pymux, state, "split-window 'sleep 30'")
        await _command(pymux, state, "split-window 'sleep 30'")
        _, second, third = window.panes

        for pane in (third, first, second):
            await _command(pymux, state, "select-pane -t %%%s" % pane.pane_id)

        await _command(pymux, state, "kill-pane")
        await once(lambda: second not in window.panes, 5.0, "the pane never closed")
        assert window.active_pane is first

        await _command(pymux, state, "kill-pane")
        await once(lambda: first not in window.panes, 5.0, "the pane never closed")
        assert window.active_pane is third
