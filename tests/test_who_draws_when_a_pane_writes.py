"""
Which clients draw when one pane writes.

**A pane that writes should wake the clients that can see it, and no
others.** Lillecarl/pymux#224 says why, and this is the gate.

prompt_toolkit already does the first half: it attaches its invalidate
handler to the controls of the layout it walks, and a client's layout
holds only the window that client looks at. So a window nobody looks at
costs nothing.

The second half is `Pymux.a_client_asked_for_a_frame`. One client's
invalidate used to ask every client for a frame, so a busy pane in one
window drew frames for a client looking at another one. It now asks the
others only for the text that names every window, which is the one
thing of theirs that a pane's write can change.

Each test that says "nobody drew" carries the same writes into a pane
that is shown, so that a zero means the writes arrived.

One of the three fails without the fix, and the other two are there on
purpose. The hidden window one records the half that already held. The
title one guards the other side: it passes before and after, and fails
if somebody takes the wake away rather than narrowing it.
"""

import asyncio
import sys

from a_session import over_a_connection
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from test_command_mode import in_a_loop

#: A pane that is still there when the test looks at it. A program that
#: exits takes its pane, and then its window, with it.
A_PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

A_SIZE = Size(rows=24, columns=80)

#: How many times each test writes. More than one, so that a frame that
#: is only the postponed one shows up as fewer frames than writes.
TIMES = 5

#: Long enough for the frame to be drawn. `max_render_postpone_time` is
#: a tenth of a second, so anything shorter measures the postponement
#: rather than the frame.
LONG_ENOUGH = 0.3


def writes(pane, text: str) -> None:
    "What the read loop of a pty does with the bytes it read."
    control = pane.terminal.terminal_control
    control.stream.feed(text)
    control.on_content_changed.fire()


async def five_writes(pane, text: str) -> None:
    for _ in range(TIMES):
        writes(pane, text)
        await asyncio.sleep(0.05)
    await asyncio.sleep(LONG_ENOUGH)


def looks_at(pymux, state):
    with set_app(state.app):
        return pymux.arrangement.get_active_window()


async def a_window_of_its_own(pymux, state):
    "A new window, made by this client, so this client looks at it."
    with set_app(state.app):
        pymux.create_window(A_PANE_THAT_STAYS)
    await asyncio.sleep(LONG_ENOUGH)


@in_a_loop
async def test_a_pane_in_a_window_nobody_looks_at_draws_nothing():
    with over_a_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        await a_window_of_its_own(pymux, state)
        await a_window_of_its_own(pymux, state)

        shown = looks_at(pymux, state)
        hidden = [w for w in pymux.arrangement.windows if w is not shown][0]

        drawn = state.app.render_counter
        await five_writes(hidden.panes[0], "nobody is looking\r\n")
        assert state.app.render_counter == drawn

        # The writes did arrive: the screen of the hidden pane has them.
        assert "nobody is looking" in hidden.panes[0].screen.page.text(0, 6)

        # And the same writes into the pane this client does look at
        # draw a frame each, so the zero above is not a dead harness.
        await five_writes(shown.panes[0], "somebody is looking\r\n")
        # More than none. Not `== TIMES`: a loaded machine coalesces
        # two writes into one frame, and this is a control and not a
        # measurement of the coalescing.
        assert state.app.render_counter > drawn


@in_a_loop
async def test_a_pane_does_not_wake_a_client_looking_elsewhere():
    with over_a_connection() as session:
        pymux = session.pymux
        a, _ = await session.attach("a", A_SIZE)
        await a_window_of_its_own(pymux, a)

        b, _ = await session.attach("b", A_SIZE)
        await a_window_of_its_own(pymux, b)

        window_of_a = looks_at(pymux, a)
        assert looks_at(pymux, b) is not window_of_a

        drawn_by_a = a.app.render_counter
        drawn_by_b = b.app.render_counter
        await five_writes(window_of_a.panes[0], "a pane of the other window\r\n")

        assert a.app.render_counter > drawn_by_a
        assert b.app.render_counter == drawn_by_b


@in_a_loop
async def test_a_title_a_pane_writes_reaches_the_other_client():
    """
    The wake still happens. It was narrowed and not removed.

    This is the widest text `what_time_moves` reads: every pane of
    every window, through `#T`. The default `window-status-format` is
    `#I:#W#F` and carries no `#T`, so this title alone does not change
    what B draws -- what does is `#W` moving, which is the name of the
    program in the pane, or a `window-status-format` a person wrote
    with `#T` in it. Lillecarl/pymux#251 holds the breadth.

    So this test does not say the wake was needed here. It says the
    wake is still there, and it fails for anybody who deletes it rather
    than narrowing it.
    """
    with over_a_connection() as session:
        pymux = session.pymux
        a, _ = await session.attach("a", A_SIZE)
        await a_window_of_its_own(pymux, a)

        b, _ = await session.attach("b", A_SIZE)
        await a_window_of_its_own(pymux, b)

        window_of_a = looks_at(pymux, a)
        drawn_by_b = b.app.render_counter

        writes(window_of_a.panes[0], "\x1b]2;a name nobody had\x07")
        await asyncio.sleep(LONG_ENOUGH)

        assert b.app.render_counter > drawn_by_b
