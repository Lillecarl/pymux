"""
tmux's mouse keys, `WheelUpPane` and the rest: a binding on one runs in
place of what pymux does with the event, and `send-keys -M` hands the
event back. Lillecarl/pyterm#1034.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
from run_commands import run_as
from session import create_session, once
from test_key_tables import run_as_client

from pymux.keys.mouse_keys import (
    CLICK_TIMEOUT,
    MouseClicks,
    MouseKey,
    MouseKind,
    MouseLocation,
    mouse_key,
)

# ----------------------------------------------------------------------
# The names.


@pytest.mark.parametrize(
    ("written", "key"),
    [
        ("WheelUpPane", MouseKey(MouseKind.WHEEL_UP, 0, MouseLocation.PANE)),
        ("wheeldownpane", MouseKey(MouseKind.WHEEL_DOWN, 0, MouseLocation.PANE)),
        ("MouseDragEnd1Pane", MouseKey(MouseKind.MOUSE_DRAG_END, 1, MouseLocation.PANE)),
        ("DoubleClick1Pane", MouseKey(MouseKind.DOUBLE_CLICK, 1, MouseLocation.PANE)),
        ("TripleClick1Pane", MouseKey(MouseKind.TRIPLE_CLICK, 1, MouseLocation.PANE)),
        ("MouseDown1StatusLeft", MouseKey(MouseKind.MOUSE_DOWN, 1, MouseLocation.STATUS_LEFT)),
        ("MouseDown3Status", MouseKey(MouseKind.MOUSE_DOWN, 3, MouseLocation.STATUS)),
        ("MouseDown11Border", MouseKey(MouseKind.MOUSE_DOWN, 11, MouseLocation.BORDER)),
        ("M-MouseDown1Pane", MouseKey(MouseKind.MOUSE_DOWN, 1, MouseLocation.PANE, meta=True)),
        ("^s-MouseUp2Pane", MouseKey(MouseKind.MOUSE_UP, 2, MouseLocation.PANE, ctrl=True, shift=True)),
    ],
)
def test_a_mouse_key_reads_as_tmux_reads_it(written, key):
    assert mouse_key(written) == key


@pytest.mark.parametrize("written", ["MouseMovePane", "MouseDown4Pane", "WheelUp1Pane", "MouseDown1", "X-WheelUpPane"])
def test_what_tmux_does_not_name_is_no_mouse_key(written):
    assert mouse_key(written) is None


def test_a_mouse_key_prints_as_tmux_prints_it():
    assert str(mouse_key("c-m-s-mousedragend1pane")) == "C-M-S-MouseDragEnd1Pane"


# ----------------------------------------------------------------------
# Counting clicks and drags.


def event(kind, button=MouseButton.LEFT, modifiers=frozenset()):
    return MouseEvent(position=Point(x=0, y=0), event_type=kind, button=button, modifiers=modifiers)


DOWN, UP, MOVE = MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP, MouseEventType.MOUSE_MOVE


def kinds(clicks, events, place="a", step=0.0):
    out = []
    for i, kind in enumerate(events):
        found = clicks.kind_of(event(kind), MouseLocation.PANE, place, now=i * step)
        out.append(None if found is None else str(found[0]))
    return out


def test_three_quick_presses_are_down_second_and_triple():
    assert kinds(MouseClicks(), [DOWN, UP, DOWN, UP, DOWN, UP]) == [
        "MouseDown",
        "MouseUp",
        "SecondClick",
        "MouseUp",
        "TripleClick",
        "MouseUp",
    ]


def test_slow_presses_are_each_a_down():
    assert kinds(MouseClicks(), [DOWN, DOWN], step=CLICK_TIMEOUT * 2) == ["MouseDown", "MouseDown"]


def test_a_press_elsewhere_starts_a_new_click():
    clicks = MouseClicks()
    clicks.kind_of(event(DOWN), MouseLocation.PANE, "a", now=0.0)
    assert clicks.kind_of(event(DOWN), MouseLocation.PANE, "b", now=0.1) == (MouseKind.MOUSE_DOWN, 1)


def test_a_move_with_a_button_drags_and_its_release_ends_the_drag():
    assert kinds(MouseClicks(), [DOWN, MOVE, MOVE, UP]) == ["MouseDown", "MouseDrag", "MouseDrag", "MouseDragEnd"]


def test_a_move_with_no_button_is_no_key():
    clicks = MouseClicks()
    assert clicks.kind_of(event(MOVE, MouseButton.NONE), MouseLocation.PANE, "a") is None


def test_a_second_press_with_no_third_stands_as_a_double_click():
    clicks = MouseClicks()
    kinds(clicks, [DOWN, DOWN])
    assert clicks.double_click_due(clicks.second_presses)
    clicks = MouseClicks()
    kinds(clicks, [DOWN, DOWN, DOWN])
    assert not clicks.double_click_due(clicks.second_presses)


# ----------------------------------------------------------------------
# Bindings.


async def two_panes(pymux, state):
    "Two panes side by side, the left one active and focused."
    with set_app(state.app):
        await run_as(pymux, state, "split-window -h")
        await run_as(pymux, state, "select-pane -L")
        state.sync_focus()
    window = pymux.arrangement.get_active_window()
    left, right = window.panes
    return window, left, right


def wheel_up(pane, state, modifiers=frozenset()):
    with set_app(state.app):
        pane.terminal.terminal_control.mouse_handler(
            event(MouseEventType.SCROLL_UP, MouseButton.NONE, modifiers=modifiers)
        )


async def test_a_mouse_key_binds_lists_and_unbinds():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "bind-key -n WheelUpPane display-message up")
        manager = pymux.key_bindings_manager
        binding = manager.binding_on("wheeluppane", "root")
        assert binding is not None and binding.written == "WheelUpPane"
        run_as_client(pymux, state, "unbind-key -n WheelUpPane")
        assert manager.binding_on("WheelUpPane", "root") is None


async def test_a_binding_on_the_wheel_runs_for_the_pane_under_the_mouse():
    async with create_session() as (pymux, state):
        window, left, right = await two_panes(pymux, state)
        run_as_client(pymux, state, "bind-key -n WheelUpPane select-pane -t=")

        wheel_up(right, state)

        await once(lambda: window.active_pane is right, 5.0, lambda: "the left pane stayed active")
        assert not right.is_copying_for(state.app), "the built-in wheel ran as well"


async def test_a_modifier_makes_another_key():
    async with create_session() as (pymux, state):
        window, left, right = await two_panes(pymux, state)
        run_as_client(pymux, state, "bind-key -n M-WheelUpPane select-pane -t=")

        wheel_up(right, state)
        wheel_up(right, state, frozenset({MouseModifier.ALT}))

        await once(lambda: window.active_pane is right, 5.0, lambda: "M-WheelUpPane did not run")


async def test_unbinding_a_mouse_key_leaves_the_built_in_product_divergence():
    async with create_session() as (pymux, state):
        _window, left, _right = await two_panes(pymux, state)
        run_as_client(pymux, state, "unbind-key -n WheelUpPane")

        wheel_up(left, state)

        assert left.is_copying_for(state.app)


async def test_with_no_binding_the_wheel_opens_copy_mode_as_before():
    async with create_session() as (pymux, state):
        _window, left, _right = await two_panes(pymux, state)

        wheel_up(left, state)

        assert left.is_copying_for(state.app)


# What two configurations of the corpus bind: the wheel over a program
# that wants no mouse selects the pane and opens copy mode, through
# `if-shell -F -t =`, `-t=` and `send-keys -M`.
CORPUS_WHEELS = """\
bind -n WheelUpPane if-shell -F -t = "#{mouse_any_flag}" "send-keys -M" "if -Ft= '#{pane_in_mode}' 'send-keys -M' 'select-pane -t=; copy-mode -e; send-keys -M'"
bind-key -T root WheelUpPane if-shell -F -t = "#{alternate_on}" "send-keys -M" "select-pane -t =; copy-mode -e; send-keys -M"
bind-key -n WheelUpPane   select-pane -t= \\; copy-mode -e \\; send-keys -M
""".splitlines()


@pytest.mark.parametrize("line", CORPUS_WHEELS)
async def test_a_corpus_wheel_binding_opens_copy_mode_on_the_pane_under_the_mouse(line, tmp_path):
    async with create_session() as (pymux, state):
        window, _left, right = await two_panes(pymux, state)
        config = tmp_path / "wheel.conf"
        config.write_text(line + "\n")
        run_as_client(pymux, state, "source-file %s" % config)
        assert pymux.startup_errors == []

        wheel_up(right, state)

        await once(
            lambda: window.active_pane is right and right.is_copying_for(state.app),
            5.0,
            lambda: "active %r, copying %r" % (window.active_pane, right.is_copying_for(state.app)),
        )


async def test_send_keys_m_outside_a_mouse_binding_is_an_error():
    async with create_session() as (pymux, state):
        run_as_client(pymux, state, "send-keys -M")
        said = [*pymux.startup_errors, *pymux.message_log, state.message or ""]
        assert any("no mouse target" in str(one) for one in said), said


async def test_a_binding_on_a_status_click_reads_the_window_it_hit():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            await run_as(pymux, state, "new-window")
        first = pymux.arrangement.windows[0]
        run_as_client(pymux, state, "bind-key -n MouseDown3Status select-window -t=")
        status = state.layout_manager.status
        part = status.part(status.window_list, MouseLocation.STATUS_DEFAULT)
        with set_app(state.app):
            part.create_content(width=80, height=1)
            first_entry = 0
            right_click = MouseEvent(
                position=Point(x=first_entry, y=0),
                event_type=MouseEventType.MOUSE_DOWN,
                button=MouseButton.RIGHT,
                modifiers=frozenset(),
            )
            part.mouse_handler(right_click)

        await once(
            lambda: pymux.arrangement.get_active_window() is first,
            5.0,
            lambda: "the window under the click was not selected",
        )
