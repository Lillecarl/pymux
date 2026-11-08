"""
Moving things between windows: `move-pane`, `join-pane`,
`link-window` and `unlink-window`.

A pane moves with `move_pane_to_window` on the arrangement; a window
goes out of the order and back with the pen of unlinked windows.
The harness starts with two windows of one pane, and the tests say
which is which. Lillecarl/pyterm#299.
"""

from __future__ import annotations

import argparse

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from run_commands import run_as
from session import create_session

from pymux.commands import CommandException


async def test_move_pane_puts_pane_in_other_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "split-window -v 'sleep 30'")

            source = pymux.arrangement.get_active_window()
            pane = source.panes[1]
            destination = pymux.arrangement.windows[0]

            await run_as(pymux, state, "move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

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

            await run_as(pymux, state, "move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

        assert len(pymux.arrangement.windows) == count - 1


async def test_join_pane_moves_into_current_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            source = pymux.arrangement.windows[0]
            pane = source.active_pane

            await run_as(pymux, state, "join-pane -s @%d" % source.window_id)

        destination = pymux.arrangement.get_active_window()
        assert pane in destination.panes
        assert pymux._window_holding(pane) is destination


async def test_unlink_takes_window_out_and_link_puts_it_back():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "new-window 'sleep 30'")
            unlinked = pymux.arrangement.get_active_window()
            window_id = unlinked.window_id
            count = len(pymux.arrangement.windows)

            await run_as(pymux, state, "unlink-window -t @%d" % window_id)

            assert unlinked not in pymux.arrangement.windows
            assert len(pymux.arrangement.windows) == count - 1
            assert unlinked.panes

            await run_as(pymux, state, "link-window -s @%d" % window_id)

        assert unlinked in pymux.arrangement.windows
        assert pymux.arrangement.get_active_window() is unlinked


async def test_last_window_refuses_to_unlink():
    async with create_session() as (pymux, state):
        from pymux.commands.unlink_window import unlink_window

        with set_app(state.app):
            await run_as(pymux, state, "unlink-window")  # Two windows: the first goes.
            with pytest.raises(CommandException):
                unlink_window(pymux, argparse.Namespace(target_window=None))


# ----------------------------------------------------------------------
# A move takes the client along, unless -d. Lillecarl/pyterm#972.


async def _with_work(pymux, state):
    "A second session, `work`, with one window. The client stays where it was."
    home = state.session
    await run_as(pymux, state, "new-session -d -s work 'sleep 30'")
    work = pymux.get_session("work")
    assert state.session is home
    return home, work


@pytest.mark.parametrize("stay", [False, True], ids=["follows", "-d stays"])
async def test_a_window_moved_to_another_session_takes_the_client_along(stay):
    with anyio.fail_after(10):
        async with create_session() as (pymux, state):
            home, work = await _with_work(pymux, state)
            with set_app(state.app):
                moving = home.arrangement.get_active_window()
            flag = "-d " if stay else ""

            await run_as(pymux, state, "move-window %s-s @%d -t work:" % (flag, moving.window_id))

            assert pymux.session_of_window(moving) is work
            assert state.session is (home if stay else work)
            if not stay:
                with set_app(state.app):
                    assert work.arrangement.get_active_window() is moving


@pytest.mark.parametrize("stay", [False, True], ids=["follows", "-d stays"])
async def test_a_pane_moved_to_another_session_leaves_its_window_and_takes_the_client_along(stay):
    with anyio.fail_after(10):
        async with create_session() as (pymux, state):
            home, work = await _with_work(pymux, state)
            with set_app(state.app):
                await run_as(pymux, state, "split-window -v 'sleep 30'")
                source = home.arrangement.get_active_window()
            pane = source.panes[1]
            (destination,) = work.arrangement.windows
            flag = "-d " if stay else ""

            await run_as(pymux, state, "move-pane %s-s %%%d -t @%d" % (flag, pane.pane_id, destination.window_id))

            assert pane not in source.panes
            assert pane in destination.panes
            assert (destination.active_pane is pane) is not stay
            assert state.session is (home if stay else work)
            if not stay:
                with set_app(state.app):
                    assert work.arrangement.get_active_window() is destination


async def test_a_pane_from_a_session_the_client_is_not_on_leaves_its_window():
    "An arrangement takes a pane out only of its own windows: the pane stayed in both."
    with anyio.fail_after(10):
        async with create_session() as (pymux, state):
            home, work = await _with_work(pymux, state)
            (source,) = work.arrangement.windows
            await run_as(pymux, state, "split-window -v -t @%d 'sleep 30'" % source.window_id)
            pane = source.panes[1]
            with set_app(state.app):
                destination = home.arrangement.get_active_window()

            await run_as(pymux, state, "move-pane -d -s %%%d -t @%d" % (pane.pane_id, destination.window_id))

            assert pane not in source.panes
            assert pane in destination.panes
