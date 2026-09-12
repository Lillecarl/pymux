"""
`respawn-pane` and `respawn-window`: a program of a pane restarts in
the place and the size the pane holds.

The rules this judges, Lillecarl/pymux#306: a pane whose program
still runs refuses without `-k`; a respawn keeps the pane in its
place in the tree -- the count and the window are the same -- and
the pane that runs there is a new one, with a new id and an empty
screen; the focus follows the replacement; and `respawn-window`
respawns the active pane of the window it names.
"""

import asyncio

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop
from pymux.commands.commands import CommandException, respawn_pane


@in_a_loop
async def test_a_pane_whose_program_runs_refuses_without_k():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            # A program that stays alive, where the plain session's
            # has gone before the first command reads it. `sleep` is
            # in the sandbox's PATH.
            pymux.handle_command("new-window 'sleep 30'")
            with pytest.raises(CommandException):
                respawn_pane(pymux, {"-k": False, "-t": False, "<target-pane>": None, "<command>": None})


@in_a_loop
async def test_the_respawn_keeps_the_place_and_replaces_the_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("split-window")
            window = pymux.arrangement.get_active_window()
            count = len(window.panes)
            old_pane = pymux.arrangement.get_active_pane()
            old_id = old_pane.pane_id

            pymux.handle_command("respawn-pane -k 'sleep 30'")

            assert len(window.panes) == count
            assert pymux.arrangement.get_active_pane().pane_id != old_id
            # The old program is gone: the kernel's reaping is a
            # moment behind the SIGKILL, so it is waited for.
            for _ in range(50):
                if old_pane.process.is_terminated:
                    break
                await asyncio.sleep(0.1)
            assert old_pane.process.is_terminated


@in_a_loop
async def test_a_pane_whose_program_ended_is_gone_and_says_so():
    """
    A pane that ends leaves the tree, and the window goes with it --
    pymux does not keep dead panes on screen. There is nothing left
    to respawn, and the command says that rather than making a pane
    nobody can see.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
            pane.process.kill()
            for _ in range(50):
                if pane.process.is_terminated:
                    break
                await asyncio.sleep(0.1)

            with pytest.raises(CommandException):
                respawn_pane(
                    pymux,
                    {"-k": False, "-t": False, "<target-pane>": None, "<command>": "sleep 30"},
                )


@in_a_loop
async def test_respawn_window_takes_the_active_pane_of_its_target():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            window = pymux.arrangement.get_active_window()
            old_id = window.active_pane.pane_id

            pymux.handle_command("respawn-window -k 'sleep 30'")

            assert window.active_pane.pane_id != old_id
