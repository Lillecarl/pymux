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

from __future__ import annotations

import argparse

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from run_commands import run_as
from session import create_session, once

from pymux.commands import CommandException
from pymux.commands.respawn_pane import respawn_pane


async def test_an_upgrading_server_refuses_a_command():
    "From the sweep to the exec, a killed pane's program would be ended and reaped by neither image. Lillecarl/pymux#740."
    async with create_session() as (pymux, state):
        panes = set(pymux.panes_by_id)
        pymux.refusing_commands = True
        await run_as(pymux, state, "kill-pane")
        assert set(pymux.panes_by_id) == panes
        assert "the server is upgrading" in state.message


async def test_pane_whose_program_runs_refuses_without_k():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            # A program that stays alive, where the plain session's
            # has gone before the first command reads it. `sleep` is
            # in the sandbox's PATH.
            await run_as(pymux, state, "new-window 'sleep 30'")
            with pytest.raises(CommandException):
                await respawn_pane(pymux, argparse.Namespace(k=False, target_pane=None, command=None))


async def test_respawn_keeps_place_and_replaces_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "split-window")
            window = pymux.arrangement.get_active_window()
            count = len(window.panes)
            old_pane = pymux.arrangement.get_active_pane()
            old_id = old_pane.pane_id

            await run_as(pymux, state, "respawn-pane -k 'sleep 30'")

            assert len(window.panes) == count
            assert pymux.arrangement.get_active_pane().pane_id != old_id
            # The old program is gone: the kernel's reaping is a
            # moment behind the SIGHUP, so it is waited for.
            for _ in range(50):
                if old_pane.process.is_terminated:
                    break
                await anyio.sleep(0.1)
            assert old_pane.process.is_terminated


async def test_respawn_keeps_the_share_of_the_split():
    "A pane resized to take more of its split keeps that share. #306."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "split-window")
            window = pymux.arrangement.get_active_window()
            pane = pymux.arrangement.get_active_pane()
            split = window._get_parent(pane)
            split.set_weight(pane, 7)

            await run_as(pymux, state, "respawn-pane -k 'sleep 30'")

            assert split.weight_of(pymux.arrangement.get_active_pane()) == 7


async def test_pane_whose_program_ended_is_gone_and_says_so():
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
            # The pane leaving the tree, and not the process ending:
            # under load the second came first, and the command still
            # found the pane. Lillecarl/pymux#705.
            await once(
                lambda: pymux.session_holding(pane) is None,
                10.0,
                "the pane never left the tree",
            )

            with pytest.raises(CommandException):
                respawn_pane(
                    pymux,
                    argparse.Namespace(k=False, target_pane=None, command="sleep 30"),
                )


async def test_respawn_window_takes_active_pane_of_its_target():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "new-window")
            window = pymux.arrangement.get_active_window()
            old_id = window.active_pane.pane_id

            await run_as(pymux, state, "respawn-window -k 'sleep 30'")

            assert window.active_pane.pane_id != old_id


async def test_respawn_restarts_in_reported_directory():
    "The new program starts where the old pane said it was."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
            pane.current_directory = "/tmp"

            seen = {}
            real_create_pane = pymux._create_pane

            async def spy(*args, **kwargs):
                seen.update(kwargs)
                return await real_create_pane(*args, **kwargs)

            pymux._create_pane = spy
            await respawn_pane(
                pymux,
                argparse.Namespace(k=True, target_pane=None, command="sleep 30"),
            )

            assert seen.get("start_directory") == "/tmp"


async def test_respawn_without_report_starts_nowhere_special():
    "A pane that never reported respawns the way a new one starts."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            seen = {}
            real_create_pane = pymux._create_pane

            async def spy(*args, **kwargs):
                seen.update(kwargs)
                return await real_create_pane(*args, **kwargs)

            pymux._create_pane = spy
            await respawn_pane(
                pymux,
                argparse.Namespace(k=True, target_pane=None, command="sleep 30"),
            )

            assert seen.get("start_directory") is None
