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

import os
import re
from types import SimpleNamespace

from prompt_toolkit.application.current import set_app
from run_commands import run_as
from session import create_session

from pymux.enums import Chooser
from pymux.hub import Heading, age
from pymux.main import Pymux
from pymux.notifications import NotificationCenter, NotificationRoutes, Route, is_click, osc99_of


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


def _hub_lines(state) -> list[str]:
    "The hub as a person reads it, one string a row."
    return "".join(fragment[1] for fragment in state.layout_manager.chooser._choose_notification_tokens()).splitlines()


async def test_hub_lists_newest_first_under_their_window():
    "Lillecarl/pymux#768: one heading for the window, its messages under it."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("notify First One")
            pymux.handle_command("notify Second Two")
            pymux.handle_command("choose-notifications")

        assert state.chooser is Chooser.NOTIFICATIONS
        window = pymux.current_session.arrangement.get_active_window()
        heading, second, first = _hub_lines(state)
        assert "%s:%s %s" % (pymux.current_session.name, window.index, window.name) in heading
        assert "Second" in second and "pymux notify" in second
        assert "First" in first


async def test_a_message_that_repeats_shows_once_with_a_count():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            for _ in range(3):
                pymux.handle_command("notify Claude Waiting")
            pymux.handle_command("choose-notifications")

        _heading, message = _hub_lines(state)
        assert "Claude - Waiting \u00d73" in message


async def test_each_window_gets_a_heading_newest_window_first():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pymux.handle_command("notify Old One")
            await run_as(pymux, state, "new-window")
            second = pymux.current_session.arrangement.get_active_window()
            pymux.handle_command("notify New Two")
            pymux.handle_command("choose-notifications")

        rows = state.layout_manager.chooser.chooser_matches()
        assert [row.window for row in rows if isinstance(row, Heading)] == [second, first]


async def test_unread_follows_the_window_s_mark():
    "A dot until somebody looks at the window the notification came from."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pane = first.active_pane
            await run_as(pymux, state, "new-window")
            pymux.notification_center.add("Built", pane_id=pane.pane_id)
            pymux.handle_command("choose-notifications")
        assert first.notified
        assert all(line[2] == "\u25cf" for line in _hub_lines(state))

        with set_app(state.app):
            state.layout_manager.chooser.choose_pointed_notification()
            pymux.handle_command("choose-notifications")
        assert not first.notified
        assert all(line[2] == " " for line in _hub_lines(state))


async def test_a_row_says_where_its_pane_is():
    "The directory the pane's shell reported, home as ~. Lillecarl/pymux#498."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
        pane.current_directory = os.path.join(os.path.expanduser("~"), "Code", "pyterm")
        pymux.notification_center.add(title="Built", body="", urgency=1, pane_id=pane.pane_id)
        with set_app(state.app):
            pymux.handle_command("choose-notifications")

        heading, _message = _hub_lines(state)
        assert " ~/Code/pyterm " in heading


async def test_a_pane_that_said_nothing_has_no_directory():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
        pane.current_directory = None
        pymux.notification_center.add(title="Built", body="", urgency=1, pane_id=pane.pane_id)
        with set_app(state.app):
            pymux.handle_command("choose-notifications")

        heading, _message = _hub_lines(state)
        assert "~" not in heading and "/" not in heading, "a path is the shell's word, and it said none"


async def test_empty_hub_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-notifications")

        rows = state.layout_manager.chooser._choose_notification_tokens()
        assert len(rows) == 1
        assert "No notifications" in rows[0][1]


async def test_search_narrows_notification_rows():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("notify Build Ready")
            pymux.handle_command("notify Test Green")
            pymux.handle_command("choose-notifications")

            state.choose_window_filter.insert_text("gree")

        _heading, message = _hub_lines(state)
        assert "Test" in message


def test_an_age_takes_the_largest_unit_that_fits():
    assert [age(s) for s in (5, 59, 60, 3599, 3600, 86400 * 2)] == ["5s", "59s", "1m", "59m", "1h", "2d"]


async def test_enter_from_hub_jumps_to_the_pane():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            assert first is not None and first.active_pane is not None
            pane_id = first.active_pane.pane_id
            await run_as(pymux, state, "notify Build Ready")
            await run_as(pymux, state, "new-window")
            assert pymux.current_session.arrangement.get_active_window() is not first
            await run_as(pymux, state, "choose-notifications")

        state.layout_manager.chooser.choose_pointed_notification()

        assert pymux.current_session.arrangement.get_active_window() is first
        assert first.active_pane is not None
        assert first.active_pane.pane_id == pane_id
        assert state.chooser is not Chooser.NOTIFICATIONS


# ----------------------------------------------------------------------
# What was seen goes after a while. Lillecarl/pymux#780.


def test_the_sweep_marks_what_was_seen_and_forgets_it_later():
    center = NotificationCenter()
    seen = center.add("Seen")
    center.add("Unseen")
    center.sweep(lambda record: 100.0 if record.id == seen.id else None, keep=60, now=120.0)
    assert [(one.title, one.seen_at) for one in center.notifications()] == [("Seen", 100.0), ("Unseen", None)]

    center.sweep(lambda record: None, keep=60, now=160.0)
    assert [one.title for one in center.notifications()] == ["Unseen"]


def test_zero_keeps_what_was_seen():
    center = NotificationCenter()
    center.add("Seen")
    center.sweep(lambda record: 100.0, keep=0, now=10_000.0)
    assert [one.seen_at for one in center.notifications()] == [100.0]


async def test_a_notification_is_seen_when_a_client_looks_at_its_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pane = first.active_pane
            await run_as(pymux, state, "new-window")
            away = pymux.notification_center.add("Away", pane_id=pane.pane_id)
            assert pymux._seen_of(away) is None

            pymux.current_session.arrangement.set_active_window(first)
            assert pymux._seen_of(away) == first.seen_at

            # One that comes while a client looks is seen at once.
            here = pymux.notification_center.add("Here", pane_id=pane.pane_id)
            assert pymux._seen_of(here) is not None

        # One with no pane has no window to look at.
        assert pymux._seen_of(pymux.notification_center.add("Script")) is not None


# ----------------------------------------------------------------------
# A click on a notification. Lillecarl/pymux#779.


def test_a_notification_asks_for_the_click_once_and_keeps_its_actions():
    routes = NotificationRoutes()
    assert routes.outgoing(7, "i=a;Hi") == "i=1:a=report;Hi"
    assert routes.outgoing(7, "i=b:a=-focus;Hi") == "i=2:a=-focus,report;Hi"
    assert routes.outgoing(7, "i=c:a=report;Hi") == "i=3:a=report;Hi"


def test_chunks_without_an_identifier_share_one_until_the_last():
    "kitty joins them while `d=0` says more follow."
    routes = NotificationRoutes()
    assert routes.outgoing(7, "d=0:p=title;Build") == "i=1:d=0:p=title:a=report;Build"
    assert routes.outgoing(7, "p=body;done") == "i=1:p=body:a=report;done"
    assert routes.outgoing(7, "p=title;Next") == "i=2:p=title:a=report;Next"


def test_a_question_asks_for_no_click():
    routes = NotificationRoutes()
    assert routes.outgoing(7, "p=?;") == "p=?;"
    assert routes.outgoing(7, "i=q:p=?;") == "i=1:p=?;"


def test_the_answer_says_what_the_program_asked():
    routes = NotificationRoutes()
    routes.outgoing(7, "i=a;Hi")
    routes.outgoing(7, "i=b:a=report,-focus;Hi")
    routes.outgoing(7, "Hi")
    assert routes.incoming("i=1;") == (Route(7, "a", reports=False, focuses=True), "i=a;")
    assert routes.incoming("i=2;") == (Route(7, "b", reports=True, focuses=False), "i=b;")
    # The program named none, and kitty answers that one as "i=0".
    assert routes.incoming("i=3;") == (Route(7, ""), "i=0;")
    assert routes.incoming("i=9;") is None


def test_an_update_after_the_last_chunk_starts_afresh():
    routes = NotificationRoutes()
    routes.outgoing(7, "i=a:a=report;One")
    routes.outgoing(7, "i=a;Two")
    route, _param = routes.incoming("i=1;")
    assert not route.reports


def test_a_click_is_only_an_answer_with_no_payload_type():
    assert is_click("i=1;") and is_click("i=1;2")
    assert not is_click("i=1:p=close;") and not is_click("i=1:p=?;a=report")


def test_pymux_s_own_notification_is_base64_and_asks_for_the_click():
    title, body = osc99_of("4", "Build", "done; 3 warnings", 2)
    assert title == "i=4:a=report:u=2:e=1:d=0:p=title;QnVpbGQ="
    assert body.startswith("i=4:a=report:u=2:e=1:p=body;")
    assert osc99_of("4", "", "Only a body", 1) == ["i=4:a=report:u=1:e=1:p=title;T25seSBhIGJvZHk="]


def _clicked(pymux, state, param):
    "kitty's answer arriving on the connection of the client `state` is."
    from pymux.server import ServerConnection

    ServerConnection._route_notification(SimpleNamespace(pymux=pymux, client_state=state), param)


async def test_a_click_on_a_notification_shows_its_pane():
    async with create_session() as (pymux, state):
        (connection,) = [each for each, value in pymux._client_states.items() if value is state]
        connection.notification_actions = frozenset({"focus", "report"})
        sent = []
        connection.forward_osc = sent.append
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pane = first.active_pane
            await run_as(pymux, state, "notify Build Ready")
            await run_as(pymux, state, "new-window")
        assert pymux.current_session.arrangement.get_active_window() is not first
        (ours,) = {re.search(r"i=(\d+)", each).group(1) for each in sent}

        _clicked(pymux, state, "i=%s;" % ours)

        assert pymux.current_session.arrangement.get_active_window() is first
        assert first.active_pane is pane


async def test_the_option_keeps_the_client_where_it_is():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pymux.forward_osc(first.active_pane, "99", "i=a;Built")
            await run_as(pymux, state, "set-option notification-click-shows-pane off")
            await run_as(pymux, state, "new-window")
            second = pymux.current_session.arrangement.get_active_window()

        _clicked(pymux, state, "i=1;")

        assert pymux.current_session.arrangement.get_active_window() is second


async def test_steal_focus_shows_the_pane_when_the_session_allows_it():
    "OSC 1337 ; StealFocus uses the same move. Lillecarl/pymux#785."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            pane = first.active_pane
            await run_as(pymux, state, "new-window")
            second = pymux.current_session.arrangement.get_active_window()

            pymux.forward_osc(pane, "1337", "StealFocus")
            assert pymux.current_session.arrangement.get_active_window() is second

            await run_as(pymux, state, "set-option steal-focus on")
            pymux.forward_osc(pane, "1337", "StealFocus")
            assert pymux.current_session.arrangement.get_active_window() is first


def test_the_query_answer_says_what_the_terminal_takes():
    from pymux.server import ServerConnection

    connection = SimpleNamespace(notification_actions=frozenset())
    ServerConnection._route_notification(connection, "i=pymux:p=?;a=focus,report:o=always:u=0,1,2:p=title,body")
    assert connection.notification_actions == {"focus", "report"}


async def test_a_terminal_that_reports_clicks_gets_osc_777_as_osc_99():
    "Only that one: a terminal that speaks OSC 777 alone gets it as it was (`test_shell_state`)."
    async with create_session() as (pymux, state):
        (connection,) = [each for each, value in pymux._client_states.items() if value is state]
        connection.notification_actions = frozenset({"report"})
        sent = []
        connection.forward_osc = sent.append
        with set_app(state.app):
            pane = pymux.arrangement.get_active_pane()
            pymux.forward_osc(pane, "777", "notify;Build;done")
        assert [each.split(";")[1] for each in sent] == [
            "i=1:a=report:u=1:e=1:d=0:p=title",
            "i=1:a=report:u=1:e=1:p=body",
        ]


def test_the_query_answer_says_whether_the_terminal_closes():
    "Lillecarl/pymux#799."
    from pymux.server import ServerConnection

    connection = SimpleNamespace(notification_actions=frozenset(), notification_closes=False)
    ServerConnection._route_notification(connection, "i=pymux:p=?;a=focus,report:c=1:o=always")
    assert connection.notification_closes
    ServerConnection._route_notification(connection, "i=pymux:p=?;a=focus,report:o=always")
    assert not connection.notification_closes


def test_a_shown_notification_is_closed_once_when_its_pane_is_seen():
    routes = NotificationRoutes()
    routes.outgoing(7, "i=a;Hi")
    routes.outgoing(7, "i=b:d=0;Half")
    ours = routes.own(8)

    assert routes.seen({8}, sent_before=0) == []
    assert routes.seen({8}, sent_before=float("inf")) == [ours]
    assert routes.seen({8}, sent_before=float("inf")) == []
    # The unfinished one was never shown.
    assert routes.seen({7}, sent_before=float("inf")) == ["1"]


async def test_a_look_at_the_window_closes_its_notification_on_the_terminal(monkeypatch):
    """
    Once a client whose terminal has the focus shows the window, and not
    on the turn the notification went out. Lillecarl/pymux#799.
    """
    monkeypatch.setattr("pymux.main.NOTIFICATION_SEEN_AFTER", 0.0)
    async with create_session() as (pymux, state):
        (connection,) = [each for each, value in pymux._client_states.items() if value is state]
        connection.notification_actions = frozenset({"report"})
        connection.notification_closes = True
        sent = []
        connection.forward_osc = sent.append
        with set_app(state.app):
            first = pymux.current_session.arrangement.get_active_window()
            await run_as(pymux, state, "new-window")
            pymux.forward_osc(first.active_pane, "99", "i=a;Built")
            state.terminal_focused = False
            pymux.current_session.arrangement.set_active_window(first)
            pymux.sync_pane_focus()
            assert not [each for each in sent if "p=close" in each]

            state.terminal_focused = True
            pymux.sync_pane_focus()
        assert [each for each in sent if "p=close" in each] == ["\x1b]99;i=1:p=close;\x1b\\"]


async def test_enter_on_a_gone_pane_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.notification_center.add("Build", "Ready", pane_id=999999)
            pymux.handle_command("choose-notifications")

        state.layout_manager.chooser.choose_pointed_notification()

        assert state.message == "The pane the notification came from is gone."
        assert state.chooser is not Chooser.NOTIFICATIONS
