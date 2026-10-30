"""
Output, a quiet spell and a notification mark a window nobody looks
at, each with a symbol of its own, and looking at the window clears
them. Lillecarl/pymux#767.

The stand-in clients are objects of our own, as in `test_bell_flags`.
"""

from __future__ import annotations

from types import SimpleNamespace

from pymux.core.arrangement import Arrangement
from pymux.core.main import Pymux
from pymux.core.window_tree import Pane, Window
from pymux.format import marks_of, window_marks

SYMBOLS = SimpleNamespace(activity_symbol="#", bell_symbol="!", silence_symbol="~", notification_symbol="?")


class _Client:
    "A client, as far as the arrangement can tell."


def _window(arrangement: Arrangement) -> tuple[Window, Pane]:
    window = Window()
    pane = Pane(None)  # type: ignore[arg-type]
    window.root.append(pane)
    window.active_pane = pane
    arrangement.windows.append(window)
    return window, pane


def test_the_first_check_takes_the_count_as_it_is():
    "A load replays every screen, and that is not output anybody missed."
    arrangement = Arrangement()
    window, pane = _window(arrangement)
    pane.revision = 40

    assert not arrangement.check_output([], 0.0)
    assert not window.activity


def test_output_nobody_saw_marks_the_window_once():
    arrangement = Arrangement()
    window, pane = _window(arrangement)
    arrangement.check_output([], 0.0)

    pane.content_changed()
    assert arrangement.check_output([], 1.0)
    assert window.activity
    pane.content_changed()
    assert not arrangement.check_output([], 2.0), "a raised mark is not news"


def test_output_in_a_watched_window_marks_nothing_later():
    arrangement = Arrangement()
    window, pane = _window(arrangement)
    client = _Client()
    arrangement._active_window_for_cli[client] = window  # type: ignore[index]
    arrangement.check_output([client], 0.0)

    pane.content_changed()
    assert not arrangement.check_output([client], 1.0)
    # The client leaves; what it saw is not news afterwards.
    assert not arrangement.check_output([], 2.0)
    assert not window.activity


def test_monitor_activity_off_marks_nothing():
    arrangement = Arrangement()
    window, pane = _window(arrangement)
    window.options.monitor_activity = False
    arrangement.check_output([], 0.0)

    pane.content_changed()
    assert not arrangement.check_output([], 1.0)


def test_a_quiet_spell_after_output_marks_silence_once():
    arrangement = Arrangement()
    window, pane = _window(arrangement)
    window.options.monitor_activity = False
    window.options.monitor_silence = 5
    arrangement.check_output([], 0.0)

    assert not arrangement.check_output([], 10.0), "quiet with no output before it says nothing"
    pane.content_changed()
    arrangement.check_output([], 11.0)
    assert not arrangement.check_output([], 15.0)
    assert arrangement.check_output([], 16.0)
    assert window.silence
    assert not arrangement.check_output([], 30.0)


def test_a_notification_marks_a_window_nobody_looks_at():
    arrangement = Arrangement()
    window, _pane = _window(arrangement)
    client = _Client()
    arrangement._active_window_for_cli[client] = window  # type: ignore[index]

    assert not arrangement.notify(window, [client])
    assert arrangement.notify(window, [])
    assert window.notified
    assert not arrangement.notify(window, []), "a raised mark is not news"


def test_monitor_notification_off_marks_nothing():
    arrangement = Arrangement()
    window, _pane = _window(arrangement)
    window.options.monitor_notification = False

    assert not arrangement.notify(window, [])


def test_a_client_that_lands_on_the_window_clears_every_mark():
    arrangement = Arrangement()
    window, _pane = _window(arrangement)
    window.activity = window.silence = window.notified = window.bell = True

    assert arrangement.get_active_window_for(_Client()) is window
    assert not (window.activity or window.silence or window.notified or window.bell)


def test_each_mark_has_its_symbol_in_tmux_s_order():
    window = Window()
    assert window_marks(SYMBOLS, window) == ""
    window.notified = window.silence = window.bell = window.activity = True
    assert window_marks(SYMBOLS, window) == "#!~?"


def test_a_session_label_names_each_mark_once():
    one, two = Window(), Window()
    one.activity = two.activity = True
    two.notified = True
    assert marks_of(SYMBOLS, [one, two]) == "#?"


async def test_a_notification_from_a_pane_marks_its_window():
    "Every source reaches the hub, and the hub marks the window."
    pymux = Pymux()
    pymux.options.test_mode = True
    async with pymux.running():
        window = await pymux.create_window("sleep 30")
        try:
            # The redraw it asks for runs with no client current, and
            # that is not somebody looking at the window.
            pymux.notification_center.add("done", pane_id=window.panes[0].pane_id)
            assert window.notified
        finally:
            window.panes[0].process.kill()
