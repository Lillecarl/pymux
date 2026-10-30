"""
The locks: `lock-client`, `lock-session` and `lock-server`.

All three cover the screen with what `lock-command` names, through
the overlay, which belongs to the session -- so the tests say the
overlay opened, and that `lock-command` is what runs in it.
Lillecarl/pymux#297.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from run_commands import run_as
from session import create_session


async def test_locking_runs_lock_command_over_whole_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "set-option lock-command 'sleep 5'")
            await run_as(pymux, state, "lock-server")

            overlay = pymux.overlay_pane
            assert overlay is not None
            assert pymux.current_session.overlay_title == "sleep 5"

            pymux.overlays.close()
            assert pymux.overlay_pane is None


async def test_all_three_locks_arrive_at_same_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "set-option lock-command 'sleep 5'")

            for command in ("lock-client", "lock-session", "lock-server"):
                await run_as(pymux, state, command)
                assert pymux.overlay_pane is not None, command
                pymux.overlays.close()
