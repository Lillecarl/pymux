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

from libpymux.protocol import Field, Packet
from prompt_toolkit.application.current import set_app
from session import create_session, once, over_connection

from pymux.commands import handle_command
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
            answer = handle_command(pymux, "split-window 'sleep 30'")
            if answer is not None:
                await answer
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


async def test_the_commands_with_no_target_reach_the_calling_pane():
    """
    Each of these read the active pane itself, so an agent's
    `rename-pane` renamed the pane a person had focused.
    Lillecarl/pymux#506.
    """
    async with two_panes() as (pymux, state, session, caller, active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=caller.pane_id):
            handle_command(pymux, "rename-pane mine")
            handle_command(pymux, "clock-mode")

        assert caller.chosen_name == "mine"
        assert caller.clock_mode
        assert active.chosen_name != "mine"
        assert not active.clock_mode


async def test_rename_pane_takes_a_target():
    "So a caller aims it at a pane that is neither its own nor the active one."
    async with two_panes() as (pymux, state, session, caller, active):
        with set_app(state.app), calling_as(pymux, session, caller_pane_id=caller.pane_id):
            handle_command(pymux, "rename-pane -t %%%d aimed" % active.pane_id)
            handle_command(pymux, "rename-pane -t %999999 nowhere")

        assert active.chosen_name == "aimed"
        assert caller.chosen_name != "aimed"


async def _answered(session, text, pane_id=None) -> tuple[str, int]:
    "What a command over a socket of its own said on stderr, and its exit code."
    got = await session.command(text, pane_id=pane_id)
    await once(lambda: any(packet[Field.CMD] == Packet.EXIT for packet in got), 10.0, "the command never answered")
    said = "".join(packet[Field.DATA] for packet in got if packet[Field.CMD] == Packet.ERR)
    return said, next(packet[Field.CODE] for packet in got if packet[Field.CMD] == Packet.EXIT)


async def test_a_command_from_nowhere_says_what_it_acts_on():
    """
    No target and no pane behind it, or one that has gone: the command
    still acts on what a person used last, as tmux does, and says which
    on stderr. It still succeeds. Lillecarl/pymux#506.
    """
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window("sleep 60")

        for pane_id, name in ((None, "far"), (999999, "gone")):
            said, code = await _answered(session, "rename-pane %s" % name, pane_id=pane_id)
            (pane,) = [pane for pane in pymux.panes_by_id.values() if pane.chosen_name == name]
            assert code == 0
            assert said == (
                "pymux: no pane named and no pane sent this; it acts on %%%d in session %s, which a person used last.\n"
                % (pane.pane_id, pymux.session_holding(pane).name)
            )


async def test_a_command_from_a_pane_or_with_a_target_says_nothing():
    async with over_connection() as session:
        pymux = session.pymux
        await pymux.panes.create_window("sleep 60")
        pane = pymux.arrangement.get_active_pane()

        assert await _answered(session, "rename-pane near", pane_id=pane.pane_id) == ("", 0)
        assert await _answered(session, "select-pane -t %%%d" % pane.pane_id) == ("", 0)
