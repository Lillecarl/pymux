"""
`select-pane -t` makes a pane the active one of the window that holds
it. It set the pane on the window a person used last, which may hold
neither it nor anything near it. Lillecarl/pymux#506.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from test_calling_pane import two_panes

from pymux.commands import handle_command


async def test_select_pane_makes_a_pane_active_in_its_own_window():
    async with two_panes() as (pymux, state, _session, caller, _active):
        first = pymux.arrangement.get_active_window()
        with set_app(state.app):
            answer = handle_command(pymux, "new-window 'sleep 30'")
            if answer is not None:
                await answer
            second = pymux.arrangement.get_active_window()
            assert second is not first
            handle_command(pymux, "select-pane -t %%%d" % caller.pane_id)

        assert first.active_pane is caller
        assert second.active_pane in second.panes
