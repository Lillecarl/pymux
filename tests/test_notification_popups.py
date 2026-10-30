"""
Notifications as boxes inside pymux, for a terminal that shows none
itself. Lillecarl/pymux#826.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from pyte.osc import Osc
from run_commands import run_as
from session import create_session, once

from pymux.enums import NotificationPopups


@asynccontextmanager
async def away_from_a_pane(shows_notifications=False):
    "A client on a second window, its terminal focused; yields the pane of the first."
    async with create_session() as (pymux, state):
        (connection,) = [each for each, value in pymux._client_states.items() if value is state]
        connection.notification_osc = Osc.NOTIFICATION if shows_notifications else None
        connection.forward_osc = lambda _sequence: None
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            await run_as(pymux, state, "new-window")
        yield pymux, state, first.active_pane


def text_of(state) -> str:
    return "".join(fragment[1] for fragment in state.layout_manager._popup_tokens())


@pytest.mark.parametrize(
    ("wanted", "shows", "popped"),
    [
        (NotificationPopups.AUTO, False, True),
        (NotificationPopups.AUTO, True, False),
        (NotificationPopups.ON, True, True),
        (NotificationPopups.OFF, False, False),
    ],
)
async def test_the_option_and_the_terminal_decide(wanted, shows, popped):
    async with away_from_a_pane(shows_notifications=shows) as (pymux, state, pane):
        state.options.notification_popups = wanted
        pymux.notification_center.add("Build", "Ready", pane_id=pane.pane_id)
        assert bool(state.popups) is popped


async def test_none_for_a_pane_in_sight():
    async with away_from_a_pane() as (pymux, state, _pane):
        with set_app(state.app):
            here = pymux.arrangement.get_active_pane()
        pymux.notification_center.add("Build", "Ready", pane_id=here.pane_id)
        assert state.popups == []


async def test_they_stack_newest_on_top():
    async with away_from_a_pane() as (pymux, state, pane):
        pymux.notification_center.add("First", "one", pane_id=pane.pane_id)
        pymux.notification_center.add("Second", "two", pane_id=pane.pane_id)
        drawn = text_of(state)
        assert drawn.index("Second") < drawn.index("First")
        assert drawn.startswith("╭") and drawn.rstrip().endswith("╯")


async def test_a_box_goes_after_its_time():
    async with away_from_a_pane() as (pymux, state, pane):
        state.options.notification_popup_seconds = 0.05
        pymux.notification_center.add("Build", "Ready", pane_id=pane.pane_id)
        assert state.popups
        await once(lambda: not state.popups, 5.0, "the box stayed")


async def test_a_box_goes_once_its_pane_is_seen():
    async with away_from_a_pane() as (pymux, state, pane):
        pymux.notification_center.add("Build", "Ready", pane_id=pane.pane_id)
        with set_app(state.app):
            await run_as(pymux, state, "previous-window")
        pymux.sync_pane_focus()
        assert state.popups == []


async def test_a_click_shows_the_pane_and_closes_the_box():
    async with away_from_a_pane() as (pymux, state, pane):
        pymux.notification_center.add("Build", "Ready", pane_id=pane.pane_id)
        (_style, _text, click) = state.layout_manager._popup_tokens()[0]
        with set_app(state.app):
            click(MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()))
            assert pymux.arrangement.get_active_pane() is pane
        assert state.popups == []
