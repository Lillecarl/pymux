"""
The locks: `lock-client`, `lock-session` and `lock-server`.

All three cover the screen with what `lock-command` names, through
the overlay, which belongs to the session -- so the tests say the
overlay opened, and that `lock-command` is what runs in it.
Lillecarl/pymux#297.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import handle_command


async def run(pymux, state, command):
    """
    Run a command as the person at this client, and wait for it.

    A key binding does not wait, but a test that did not would assert
    before the window exists. The end state is the same either way.
    """
    with set_app(state.app):
        answer = handle_command(pymux, command)
        if answer is not None:
            await answer


async def test_locking_runs_lock_command_over_whole_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-option lock-command 'sleep 5'")
            await run(pymux, state, "lock-server")

            overlay = pymux.overlay_pane
            assert overlay is not None
            assert pymux.current_session.overlay_title == "sleep 5"

            pymux.close_overlay()
            assert pymux.overlay_pane is None


async def test_all_three_locks_arrive_at_same_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "set-option lock-command 'sleep 5'")

            for command in ("lock-client", "lock-session", "lock-server"):
                await run(pymux, state, command)
                assert pymux.overlay_pane is not None, command
                pymux.close_overlay()
