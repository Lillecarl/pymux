"""
Moving things between windows: `move-pane`, `join-pane`,
`link-window` and `unlink-window`.

A pane moves with `move_pane_to_window` on the arrangement; a window
goes out of the order and back with the pen of unlinked windows.
The harness starts with two windows of one pane, and the tests say
which is which. Lillecarl/pymux#297.
"""

import argparse

import pytest
from prompt_toolkit.application.current import set_app

from pymux.commands import CommandException
from session import create_session, in_a_loop


@in_a_loop
async def test_move_pane_puts_the_pane_in_the_other_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("split-window -v 'sleep 30'")

            source = pymux.arrangement.get_active_window()
            pane = source.panes[1]
            destination = pymux.arrangement.windows[0]

            pymux.handle_command("move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

        assert pane not in source.panes
        assert pane in destination.panes
        assert source.panes


@in_a_loop
async def test_an_emptied_window_is_gone():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            count = len(pymux.arrangement.windows)
            source = pymux.arrangement.windows[0]
            pane = source.active_pane
            destination = pymux.arrangement.windows[1]

            pymux.handle_command("move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

        assert len(pymux.arrangement.windows) == count - 1


@in_a_loop
async def test_join_pane_moves_into_the_current_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            source = pymux.arrangement.windows[0]
            pane = source.active_pane

            pymux.handle_command("join-pane -s @%d" % source.window_id)

        destination = pymux.arrangement.get_active_window()
        assert pane in destination.panes
        assert pymux._window_holding(pane) is destination


@in_a_loop
async def test_unlink_takes_the_window_out_and_link_puts_it_back():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window 'sleep 30'")
            unlinked = pymux.arrangement.get_active_window()
            window_id = unlinked.window_id
            count = len(pymux.arrangement.windows)

            pymux.handle_command("unlink-window -t @%d" % window_id)

            assert unlinked not in pymux.arrangement.windows
            assert len(pymux.arrangement.windows) == count - 1
            assert unlinked.panes

            pymux.handle_command("link-window -s @%d" % window_id)

        assert unlinked in pymux.arrangement.windows
        assert pymux.arrangement.get_active_window() is unlinked


@in_a_loop
async def test_the_last_window_refuses_to_unlink():
    async with create_session() as (pymux, state):
        from pymux.commands.unlink_window import unlink_window

        with set_app(state.app):
            pymux.handle_command("unlink-window")  # Two windows: the first goes.
            with pytest.raises(CommandException):
                unlink_window(pymux, argparse.Namespace(target_window=None))
