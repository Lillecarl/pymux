"""
The notifications hub: what it keeps, what feeds it, and the chooser
that shows it.

The hub lists what every source recorded -- a pane's OSC 99, a
`notify` call -- newest first, with when each arrived and which pane
it came from. Enter takes the client to that pane. The pipe the
terminal reads stays as it was: collecting is beside forwarding, and
not instead of it.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import handle_command
from pymux.main import Pymux
from pymux.notifications import NotificationCenter


class FakePane:
    "A pane is named by its id when a notification is recorded."

    def __init__(self, pane_id=7):
        self.pane_id = pane_id


def test_records_come_back_oldest_first():
    center = NotificationCenter()
    first = center.add("One")
    second = center.add("Two")
    assert center.notifications() == [first, second]
    assert second.id == first.id + 1


def test_a_noisy_pane_does_not_grow_the_hub_without_end():
    center = NotificationCenter(limit=2)
    center.add("One")
    center.add("Two")
    center.add("Three")
    assert [one.title for one in center.notifications()] == ["Two", "Three"]


def test_osc99_without_identifier_stands_alone():
    """
    Nothing assembles chunks without one, so each is its own
    notification, by the type its metadata names.
    """
    center = NotificationCenter()
    title = center.add_osc99(7, "p=title;Build done")
    body = center.add_osc99(7, "p=body;Three files")
    assert title is not None and body is not None
    assert (title.title, title.body) == ("Build done", "")
    assert (body.title, body.body) == ("", "Three files")


def test_osc99_chunks_with_an_identifier_assemble():
    center = NotificationCenter()
    assert center.add_osc99(7, "i=9:p=title:d=0;Part one") is None
    assert center.notifications() == []
    record = center.add_osc99(7, "i=9:p=body;Part two")
    assert record is not None
    assert (record.title, record.body, record.pane_id) == (
        "Part one",
        "Part two",
        7,
    )


def test_osc99_urgency_defaults_to_normal():
    center = NotificationCenter()
    plain = center.add_osc99(7, "p=title;Hey")
    loud = center.add_osc99(7, "i=1:u=2:p=title;Fire")
    bogus = center.add_osc99(7, "i=2:u=9:p=title;Hey")
    assert plain is not None and loud is not None and bogus is not None
    assert (plain.urgency, loud.urgency, bogus.urgency) == (1, 2, 1)


def test_osc99_is_collected_for_the_hub():
    pymux = Pymux()
    pymux._client_states = {}
    pane = FakePane()
    pymux.forward_osc(pane, "99", "i=7:p=title:d=0;Part one")
    assert pymux.notification_center.notifications() == []
    pymux.forward_osc(pane, "99", "i=7:p=body;Part two")
    (record,) = pymux.notification_center.notifications()
    assert (record.title, record.body, record.pane_id) == (
        "Part one",
        "Part two",
        7,
    )


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


async def test_notify_records_title_body_and_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            window = pymux.current_session.arrangement.get_active_window()
            assert window is not None and window.active_pane is not None
            wanted = window.active_pane.pane_id
            pymux.handle_command("notify Build Ready")

        (record,) = pymux.notification_center.notifications()
        assert record.title == "Build"
        assert record.body == "Ready"
        assert record.pane_id == wanted


async def test_notify_takes_urgency():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("notify Fire Down -u critical")

        (record,) = pymux.notification_center.notifications()
        assert record.urgency == 2


async def test_hub_lists_newest_first():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("notify First One")
            pymux.handle_command("notify Second Two")
            pymux.handle_command("choose-notifications")

        assert state.choose_notifications
        rows = state.layout_manager._choose_notification_tokens()
        assert len(rows) == 2
        assert "Second" in rows[0][1]
        assert "First" in rows[1][1]


async def test_empty_hub_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-notifications")

        rows = state.layout_manager._choose_notification_tokens()
        assert len(rows) == 1
        assert "No notifications" in rows[0][1]


async def test_search_narrows_notification_rows():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("notify Build Ready")
            pymux.handle_command("notify Test Green")
            pymux.handle_command("choose-notifications")

            state.choose_window_filter.insert_text("gree")

        rows = state.layout_manager._choose_notification_tokens()
        assert len(rows) == 1
        assert "Test" in rows[0][1]


async def test_enter_from_hub_jumps_to_the_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            assert first is not None and first.active_pane is not None
            pane_id = first.active_pane.pane_id
            await run(pymux, state, "notify Build Ready")
            await run(pymux, state, "new-window")
            assert pymux.current_session.arrangement.get_active_window() is not first
            await run(pymux, state, "choose-notifications")

        state.layout_manager.choose_pointed_notification()

        assert pymux.current_session.arrangement.get_active_window() is first
        assert first.active_pane is not None
        assert first.active_pane.pane_id == pane_id
        assert not state.choose_notifications


async def test_enter_on_a_gone_pane_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.notification_center.add("Build", "Ready", pane_id=999999)
            pymux.handle_command("choose-notifications")

        state.layout_manager.choose_pointed_notification()

        assert state.message == "The pane the notification came from is gone."
        assert not state.choose_notifications
