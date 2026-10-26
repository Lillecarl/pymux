"""
A bell nobody saw flags its window and its pane, the way tmux's
`window_bell_flag` does, and looking at them clears both.
Lillecarl/pymux#501.

The stand-in clients are objects of our own: `_active_window_for_cli`
keys on the application and does nothing else with it.
"""

from __future__ import annotations

import anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture

from pymux.arrangement import Arrangement, Pane, Window
from pymux.format import format_pymux_string


class _Client:
    "A client, as far as the arrangement can tell."


def _window_of_two(arrangement: Arrangement) -> tuple[Window, Pane, Pane]:
    window = Window()
    left, right = Pane(None), Pane(None)  # type: ignore[arg-type]
    window.root.extend([left, right])
    window.active_pane = left
    arrangement.windows.append(window)
    return window, left, right


def test_a_bell_in_a_window_nobody_looks_at_flags_the_window_and_the_pane():
    arrangement = Arrangement()
    window, left, _right = _window_of_two(arrangement)

    assert arrangement.ring(window, left, [])
    assert (window.bell, left.bell) == (True, True)
    assert not arrangement.ring(window, left, []), "a raised flag is not news"


def test_a_bell_in_the_active_pane_of_a_watched_window_flags_nothing():
    arrangement = Arrangement()
    window, left, _right = _window_of_two(arrangement)
    client = _Client()
    arrangement._active_window_for_cli[client] = window  # type: ignore[index]

    assert not arrangement.ring(window, left, [client])
    assert (window.bell, left.bell) == (False, False)


def test_a_bell_beside_the_active_pane_flags_that_pane_alone():
    arrangement = Arrangement()
    window, _left, right = _window_of_two(arrangement)
    client = _Client()
    arrangement._active_window_for_cli[client] = window  # type: ignore[index]

    assert arrangement.ring(window, right, [client])
    assert (window.bell, right.bell) == (False, True)

    window.active_pane = right
    assert not right.bell


def test_a_detached_client_sees_nothing():
    arrangement = Arrangement()
    window, left, _right = _window_of_two(arrangement)
    arrangement._active_window_for_cli[_Client()] = window  # type: ignore[index]

    assert arrangement.ring(window, left, [])
    assert window.bell


def test_monitor_bell_off_flags_nothing():
    arrangement = Arrangement()
    window, left, _right = _window_of_two(arrangement)
    window.options.monitor_bell = False

    assert not arrangement.ring(window, left, [])
    assert (window.bell, left.bell) == (False, False)


def test_a_client_that_lands_on_the_window_clears_its_flags():
    arrangement = Arrangement()
    window, left, _right = _window_of_two(arrangement)
    arrangement.ring(window, left, [])

    assert arrangement.get_active_window_for(_Client()) is window
    assert (window.bell, left.bell) == (False, False)


async def test_a_shell_that_rings_in_another_window_shows_in_the_status_line(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client_state
        app = client.app
        windows = client.session.arrangement.windows
        (first,) = windows
        with set_app(app):
            pymux.handle_command("new-window")
        await _until(lambda: len(windows) == 2)
        second = windows[1]
        shell = second.active_pane
        with set_app(app):
            pymux.handle_command("select-window -t %s" % first.index)
            await _until(lambda: client.session.arrangement.get_active_window() is first)

        shell.process.write_input("printf '\\a'\r")
        await _until(lambda: second.bell)

        assert shell.bell
        with set_app(app):
            flags = format_pymux_string(
                pymux,
                "#{window_bell_flag}#{pane_bell_flag}#{window_flags}",
                window=second,
                pane=shell,
                session=client.session,
                client=client,
            )
            assert flags.rstrip("-") == "11!"

            pymux.handle_command("select-window -t %s" % second.index)
            await _until(lambda: client.session.arrangement.get_active_window() is second)
        assert (second.bell, shell.bell) == (False, False)


async def _until(condition, seconds: float = 10.0) -> None:
    with anyio.fail_after(seconds):
        while not condition():
            await anyio.sleep(0.02)
