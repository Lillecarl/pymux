"""
Moving things between windows: `move-pane`, `join-pane`,
`link-window` and `unlink-window`.

A pane moves with `move_pane_to_window` on the arrangement; a window
goes out of the order and back with the pen of unlinked windows.
The harness starts with two windows of one pane, and the tests say
which is which. Lillecarl/pymux#297.
"""

from __future__ import annotations

import argparse

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException, handle_command


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


async def test_move_pane_puts_pane_in_other_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "split-window -v 'sleep 30'")

            source = pymux.arrangement.get_active_window()
            pane = source.panes[1]
            destination = pymux.arrangement.windows[0]

            await run(pymux, state, "move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

        assert pane not in source.panes
        assert pane in destination.panes
        assert source.panes


async def test_emptied_window_is_gone():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            count = len(pymux.arrangement.windows)
            source = pymux.arrangement.windows[0]
            pane = source.active_pane
            destination = pymux.arrangement.windows[1]

            await run(pymux, state, "move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

        assert len(pymux.arrangement.windows) == count - 1


async def test_join_pane_moves_into_current_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            source = pymux.arrangement.windows[0]
            pane = source.active_pane

            await run(pymux, state, "join-pane -s @%d" % source.window_id)

        destination = pymux.arrangement.get_active_window()
        assert pane in destination.panes
        assert pymux._window_holding(pane) is destination


async def test_unlink_takes_window_out_and_link_puts_it_back():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run(pymux, state, "new-window 'sleep 30'")
            unlinked = pymux.arrangement.get_active_window()
            window_id = unlinked.window_id
            count = len(pymux.arrangement.windows)

            await run(pymux, state, "unlink-window -t @%d" % window_id)

            assert unlinked not in pymux.arrangement.windows
            assert len(pymux.arrangement.windows) == count - 1
            assert unlinked.panes

            await run(pymux, state, "link-window -s @%d" % window_id)

        assert unlinked in pymux.arrangement.windows
        assert pymux.arrangement.get_active_window() is unlinked


async def test_last_window_refuses_to_unlink():
    async with create_session() as (pymux, state):
        from pymux.commands.unlink_window import unlink_window

        with set_app(state.app):
            await run(pymux, state, "unlink-window")  # Two windows: the first goes.
            with pytest.raises(CommandException):
                unlink_window(pymux, argparse.Namespace(target_window=None))
