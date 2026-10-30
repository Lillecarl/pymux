"""
When a client draws a frame. A pane that writes wakes the clients that
can see it by itself; this wakes the rest when their text moved, keeps
the clock and the `#{...}` variables current, paces each client to its
frame rate, and draws a client whole when asked to.
`Pymux.invalidate` stays the one call that says something changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app

from .enums import RenderMode, Woke
from .log import logger

if TYPE_CHECKING:
    from .client_state import ClientState
    from .main import Pymux

__all__ = ["Waking"]


class Waking:
    "Wakes each client for the frames it needs, and no more."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def refresh_what_time_moves(self, but_not: Application[Any] | None = None) -> None:
        """
        Ask for a frame from each client whose screen moved by itself.

        The clock in the status line is the reason the auto refresh
        exists. `#{...}` variables in the status line and in the
        titlebar of a pane are the rest of it, and `clock-mode` draws a
        clock inside a pane.

        A frame is 279,645 bytecode instructions, and the text that
        time moves is 2,371 of them. So this reads the text and draws
        only when it differs from what the last frame drew. A clock
        that says `%H:%M` changes once a minute, and the interval is
        four seconds, so fourteen of every fifteen ticks stop here.
        Lillecarl/pymux#154.

        A pane that writes invalidates its own client. It never needed
        the clock. Lillecarl/pymux#117.

        `but_not` is the application that already asked for a frame, so
        that `client_asked_for_frame` can ask about the others and
        leave that one alone.
        """
        for client_state in self.pymux._client_states.values():
            # Every scheduled redraw records the tick, for its own
            # client and the skipped one alike: the renderer compares
            # it against the input since, and skips a frame that
            # input arrived after. Lillecarl/pymux#524.
            client_state.schedule_tick = client_state.input_tick
            if client_state.app is but_not:
                continue

            # A `#{...}` variable asks which window this client looks
            # at, so each client reads its own text.
            with set_app(client_state.app):
                text = client_state.layout_manager.what_time_moves()

            if text != client_state.last_time_text:
                client_state.last_time_text = text
                # Named here and not through `invalidate`, because this
                # wakes the one client whose text moved and not all of
                # them. `Woke` says why the log carries the reason.
                logger.debug(
                    "Drawing 1 of the clients: the text that time moves changed to %r",
                    text,
                )
                client_state.app.invalidate()

    def client_asked_for_frame(self, app: Application[Any]) -> None:
        """
        What one client's own invalidate means for the other clients.

        A pane that writes arrives here. prompt_toolkit invalidates the
        application whose layout holds that pane, and no other one:
        `Application._update_invalidate_events` attaches its handler to
        the controls it walks, and a client's layout holds the window
        it looks at. So the wake of the clients that can see the change
        already happened before this runs.

        Two things are left for the clients that cannot see it, and
        both are why this is not simply nothing. Lillecarl/pymux#224.

        - Every client's status line names the other windows, through
          `window-status-format`. `what_time_moves` reads exactly that
          text, so the clients whose window list moved wake and the
          rest do not.
        - The four syncs below. A program pushes kitty keyboard flags
          or asks for a pointer by writing, and this is the path that
          notices.

        With one client there are no others, so the common case pays
        for the syncs and nothing else.
        """
        self.pymux.counters.invalidated(Woke.APPLICATION)
        # DEBUG: this is the line that says a pane is animating. A
        # server drew eleven frames a second with nobody typing, and a
        # line each is what made one log 86 MB in four days.
        # Lillecarl/pymux#248.
        logger.debug("Drawing 1 of the clients: %s", Woke.APPLICATION)

        self.refresh_what_time_moves(but_not=app)

        self.pymux.terminals.sync_kitty_flags()
        self.pymux.terminals.sync_pointer_shape()
        self.pymux.terminals.sync_progress()
        self.pymux.terminals.sync_keyboard_source_flags()

    async def auto_refresh(self) -> None:
        """
        Refresh the clients every `status_interval` seconds.

        On the loop, and not on a thread of its own. The refresh reads
        which window each client looks at, and `set_app` answers that
        by writing an `AppSession` that every client shares. A thread
        that writes it while a render reads it hands that render
        another client's application. Lillecarl/pymux#155.

        The interval is read each turn, so a `set-option
        status-interval` reaches the tick after this one. One timer
        serves every session, so it ticks at the shortest any of them
        asks for.
        """
        while True:
            await anyio.sleep(
                min(
                    (s.options.status_interval for s in self.pymux.sessions),
                    default=self.pymux.session_defaults.status_interval,
                )
            )
            try:
                self.refresh_what_time_moves()
            except Exception:
                # The clock must not take the server with it. In a task
                # group an exception cancels the siblings, and the
                # siblings here are every client of the server.
                logger.exception("The refresh on the clock failed.")

    def apply_render_mode(self) -> None:
        """
        Draw every client and every pane the way `render-mode` says, from
        the next frame on, and draw that frame whole.
        """
        reference = self.pymux.options.render_mode is RenderMode.REFERENCE
        for pane in self.pymux.panes_by_id.values():
            pane.terminal.terminal_control.keep_rows = not reference
        for client_state in self.pymux._client_states.values():
            client_state.app.renderer.reference = reference
            self.redraw(client_state)

    def redraw(self, client_state: ClientState) -> None:
        """
        Draw this client's terminal again from nothing.

        A frame normally writes what changed since the one before, so a
        cell the terminal lost -- or one the diff got wrong -- stays lost
        until something writes over it. This forgets the frame before:
        the next frame homes the cursor, erases the screen and writes
        every cell. Not `Renderer.clear`, which leaves the alternate
        screen and asks the terminal where its cursor is.

        It enters the alternate screen again, which a terminal can have
        left behind pymux's back: iTerm2 leaves it on a prompt start it
        reads, and the frames after went to its main screen. On a
        terminal that never left, it clears a screen this redraws
        anyway. Lillecarl/pymux#791.

        The erase takes kitty's placements with it, so the images go too,
        and each is sent and placed again. A window pane left as it was
        gets only its damage, on an image nothing shows, and its pane
        shows the background.
        """
        if client_state.connection is not None:
            client_state.connection.graphics.reset()
        renderer = client_state.app.renderer
        renderer.reset(leave_alternate_screen=False)
        if renderer.full_screen:
            # The switch clears in the colours set last; a frame's are not.
            renderer.output.reset_attributes()
            renderer.output.enter_alternate_screen()
            renderer.output.flush()
        client_state.app.invalidate()

    def sync_frame_rate(self) -> None:
        """
        Cap each client at the frame rate of the window it looks at.

        `Application.min_redraw_interval` is prompt_toolkit's own knob
        and it is the right one: a redraw that arrives too soon is not
        dropped, it is held until the interval is up and then drawn. So
        a cap loses no frame, it only refuses to draw the same thing
        twice in a thirtieth of a second.

        Never raises: this runs on the invalidate path, also for
        headless servers without a running prompt_toolkit application.
        """
        for client_state in self.pymux._client_states.values():
            try:
                with set_app(client_state.app):
                    window = self.pymux.arrangement.get_active_window()
            except Exception:
                # No window yet, or no application that ever ran.
                continue

            rate = window.options.frame_rate if window is not None else 0
            client_state.frame_interval = (1.0 / rate) if rate else None
            client_state.pace()
