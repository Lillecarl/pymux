"""
Untargeted commands from a pane reach that pane.

A command typed in a pane names it in the run-command packet, and the
temporary client it runs under keeps the id. Without a target such a
command defaults to the calling pane instead of the active one, so a
program in a pane reaches its own pane without naming it -- tmux
answers `TMUX_PANE` the same way. An explicit target always wins, a
real client keeps its focus, and a pane that died since falls back to
the active one.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands.common import find_pane


@contextlib.contextmanager
def calling_as(pymux, session, temporary=True, caller_pane_id=None):
    """
    Answer `get_client_state` with a client of the given kind, looking
    at the given session.

    A temporary one with an id is a socket command from a pane; a
    real one is a person at a terminal, looking at their focus.
    """
    real = pymux.get_client_state
    pymux.get_client_state = lambda: SimpleNamespace(
        temporary=temporary, caller_pane_id=caller_pane_id, session=session
    )
    try:
        yield
    finally:
        pymux.get_client_state = real


@asynccontextmanager
async def two_panes():
    """
    A session whose window holds two panes: the calling one, and the
    active one a split focused.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("split-window 'sleep 30'")
            window = pymux.arrangement.get_active_window()
            assert len(window.panes) == 2
            active = pymux.arrangement.get_active_pane()
            caller = next(pane for pane in window.panes if pane is not active)
            yield pymux, state, pymux.current_session, caller, active


async def test_calling_pane_wins_over_active_pane():
    async with two_panes() as (pymux, state, session, caller, _active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=caller.pane_id):
            assert find_pane(pymux, None) is caller
            assert find_pane(pymux, "") is caller


async def test_explicit_target_wins_over_calling_pane():
    async with two_panes() as (pymux, state, session, caller, active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=caller.pane_id):
            assert find_pane(pymux, "%%%d" % active.pane_id) is active


async def test_real_client_keeps_its_focus():
    async with two_panes() as (pymux, state, session, caller, active):
        with set_app(state.app), calling_as(pymux, session, temporary=False, caller_pane_id=caller.pane_id):
            assert find_pane(pymux, None) is active


async def test_dead_calling_pane_falls_back_to_active():
    async with two_panes() as (pymux, state, session, _caller, active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=999999):
            assert find_pane(pymux, None) is active


async def test_command_without_caller_keeps_active_pane():
    async with two_panes() as (pymux, state, session, _caller, active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=None):
            assert find_pane(pymux, None) is active
