"""
The locks: `lock-client`, `lock-session` and `lock-server`.

All three cover the screen with what `lock-command` names, through
the overlay, which belongs to the session -- so the tests say the
overlay opened, and that `lock-command` is what runs in it.
Lillecarl/pymux#297.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_locking_runs_the_lock_command_over_the_whole_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option lock-command 'sleep 5'")
            pymux.handle_command("lock-server")

            overlay = pymux.overlay_pane
            assert overlay is not None
            assert pymux.overlay_title == "sleep 5"

            pymux.close_overlay()
            assert pymux.overlay_pane is None


@in_a_loop
async def test_all_three_locks_arrive_at_the_same_screen():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option lock-command 'sleep 5'")

            for command in ("lock-client", "lock-session", "lock-server"):
                pymux.handle_command(command)
                assert pymux.overlay_pane is not None, command
                pymux.close_overlay()
