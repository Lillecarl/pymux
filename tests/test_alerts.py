"""
A bell, activity or silence rings and tells the clients of its session
that `*-action` covers, the way tmux's `alerts_set_message` does.
Lillecarl/pyterm#1035.

The clients and sessions are stand-ins: `alert` reads a client's
session, application and message, and a session's options and
arrangement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pymux.core.alerts import alert
from pymux.core.arrangement import Arrangement
from pymux.core.pane import Pane
from pymux.core.window_tree import Window
from pymux.enums import Alert, AlertAction, Visual
from pymux.options.session_options import SessionOptions


class _Output:
    def __init__(self) -> None:
        self.rang = 0

    def bell(self) -> None:
        self.rang += 1


class _App:
    def __init__(self) -> None:
        self.output = _Output()


def _session() -> SimpleNamespace:
    return SimpleNamespace(options=SessionOptions(), arrangement=Arrangement())


def _client(session: SimpleNamespace, window: Window) -> SimpleNamespace:
    client = SimpleNamespace(session=session, app=_App(), message=None)
    session.arrangement._active_window_for_cli[client.app] = window
    return client


def _two_windows() -> tuple[SimpleNamespace, Window, SimpleNamespace, SimpleNamespace]:
    "A session of two windows, a client on each; the alert is in the first."
    session = _session()
    rang, elsewhere = Window(), Window()
    rang.index, elsewhere.index = 1, 2
    session.arrangement.windows.extend([rang, elsewhere])
    return session, rang, _client(session, rang), _client(session, elsewhere)


@pytest.mark.parametrize(
    ("action", "on_the_window", "elsewhere"),
    [
        (AlertAction.ANY, 1, 1),
        (AlertAction.CURRENT, 1, 0),
        (AlertAction.OTHER, 0, 1),
        (AlertAction.NONE, 0, 0),
    ],
)
def test_bell_action_picks_the_clients_that_ring(action, on_the_window, elsewhere):
    session, window, looking, away = _two_windows()
    session.options.bell_action = action

    alert([looking, away], session, window, Alert.BELL)

    assert (looking.app.output.rang, away.app.output.rang) == (on_the_window, elsewhere)
    assert looking.message is away.message is None, "visual-bell off rings and says nothing"


def test_a_client_of_another_session_hears_nothing():
    session, window, looking, _away = _two_windows()
    other = _session()
    stranger = _client(other, Window())

    alert([looking, stranger], session, window, Alert.BELL)

    assert (looking.app.output.rang, stranger.app.output.rang) == (1, 0)


def test_visual_bell_on_says_where_instead_of_ringing():
    session, window, looking, away = _two_windows()
    session.options.visual_bell = Visual.ON

    assert alert([looking, away], session, window, Alert.BELL)

    assert (looking.app.output.rang, away.app.output.rang) == (0, 0)
    assert looking.message == "Bell in current window"
    assert away.message == "Bell in window 1"


def test_visual_both_rings_and_says():
    session, window, _looking, away = _two_windows()
    session.options.visual_silence = Visual.BOTH

    alert([away], session, window, Alert.SILENCE)

    assert away.app.output.rang == 1
    assert away.message == "Silence in window 1"


def test_monitor_bell_off_rings_nobody():
    session, window, looking, away = _two_windows()
    window.options.monitor_bell = False

    assert not alert([looking, away], session, window, Alert.BELL)
    assert (looking.app.output.rang, away.app.output.rang) == (0, 0)


def test_activity_in_a_watched_window_alerts_nobody_product_divergence():
    "tmux's activity-action any or current acts on output in the window a client shows; pymux marks only unseen ones."
    session, window, looking, _away = _two_windows()
    pane = Pane(None)  # type: ignore[arg-type]
    window.root.append(pane)
    window.active_pane = pane
    session.options.activity_action = AlertAction.ANY
    session.arrangement.check_output([looking.app], 0.0)

    pane.content_changed()

    assert not session.arrangement.check_output([looking.app], 1.0)


def test_activity_rings_nobody_unless_asked():
    "pymux watches activity unasked, so its default action is none, where tmux's is other."
    session, window, _looking, away = _two_windows()
    session.options.visual_activity = Visual.ON

    alert([away], session, window, Alert.ACTIVITY)
    assert away.message is None

    session.options.activity_action = AlertAction.OTHER
    alert([away], session, window, Alert.ACTIVITY)
    assert away.message == "Activity in window 1"
