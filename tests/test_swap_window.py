"""
`swap-window`: the active window and the window a target names trade
indexes, and the window swapped into the active place takes the focus
unless `-d` keeps it. tmux's move-window refuses an occupied index,
which is every index a chooser can name; the swap is what its tree
template does. Lillecarl/pymux#296.
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


async def test_windows_trade_indexes_and_destination_takes_focus():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-window")  # index 3, and it is active

            first, second, third = pymux.arrangement.windows

            await run(pymux, state, "swap-window -t :1")

            assert third.index == 1
            assert first.index == 3
            # The window swapped into the active one's place takes
            # the focus.
            assert pymux.arrangement.get_active_window() is first


async def test_d_keeps_active_window_active():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-window")

            third = pymux.arrangement.windows[2]

            await run(pymux, state, "swap-window -t :1 -d")

            assert third.index == 1
            assert pymux.arrangement.get_active_window() is third


async def test_relative_target_counts_from_active_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-window")  # index 3, and it is active

            second = pymux.arrangement.windows[1]
            third = pymux.arrangement.windows[2]

            # The spell of the M-J and M-K of a tmux config.
            await run(pymux, state, "swap-window -t -1")

            assert third.index == 2
            assert second.index == 3
            assert pymux.arrangement.get_active_window() is second


async def test_swap_with_itself_does_nothing():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-window")

            windows = list(pymux.arrangement.windows)

            await run(pymux, state, "swap-window -t :3")

            assert pymux.arrangement.windows == windows
