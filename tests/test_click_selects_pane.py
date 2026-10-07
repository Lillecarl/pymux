"""
A click in a pane selects it.

The widget moves the layout focus on a click in a pane that had
none; the arrangement follows, or the next keypress syncs the
layout focus straight back to the pane that is active. A press
selects nothing -- the focus lands on the release, the way the
clock does -- and a wheel over a pane that has no focus does
nothing at all. Lillecarl/pymux#527.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from session import create_session

from pymux.commands import handle_command


def _click(kind):
    return MouseEvent(position=Point(x=0, y=0), event_type=kind, button=MouseButton.LEFT, modifiers=frozenset())


async def _two_panes_left_active(pymux, state):
    "Two panes side by side, the left one active and focused, and the window that holds them."
    with set_app(state.app):
        await run(pymux, state, "split-window -h")
        await run(pymux, state, "select-pane -L")
        state.sync_focus()
    window = pymux.arrangement.get_active_window()
    left, right = window.panes
    assert window.active_pane is left
    assert state.app.layout.has_focus(left.terminal)
    return window, left, right


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


async def test_a_press_in_an_unfocused_pane_selects_nothing():
    async with create_session() as (pymux, state):
        window, left, right = await _two_panes_left_active(pymux, state)
        with set_app(state.app):
            right.terminal.terminal_control.mouse_handler(_click(MouseEventType.MOUSE_DOWN))
        assert window.active_pane is left
        assert state.app.layout.has_focus(left.terminal)


async def test_a_click_in_an_unfocused_pane_selects_it():
    async with create_session() as (pymux, state):
        window, left, right = await _two_panes_left_active(pymux, state)
        with set_app(state.app):
            right.terminal.terminal_control.mouse_handler(_click(MouseEventType.MOUSE_UP))
        assert window.active_pane is right
        assert state.app.layout.has_focus(right.terminal)


async def test_a_click_on_a_pane_in_no_window_selects_nothing():
    "An overlay pane sits in no window, and a gone one neither."
    async with create_session() as (pymux, state):
        window, left, right = await _two_panes_left_active(pymux, state)
        with set_app(state.app):
            pymux.arrangement.remove_pane(right)
            # The hook itself, past the focus the layout refuses a
            # pane it no longer draws: a click cannot reach one, but
            # the guard is what an overlay pane leans on.
            right.terminal.terminal_control.on_mouse_focus()
        assert window.active_pane is left
