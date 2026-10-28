"""
The choosers of one client: the window chooser, a bar across the top,
and the boxes that choose a buffer, an option, a notification or a
job. What is being chosen lives on `ClientState`; this draws it and
acts on the row it points at. Lillecarl/pymux#775.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application import get_app
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    Container,
    HSplit,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

from pymux import arrangement

from .actions import Kind, Origin, note, started_by
from .boxes import BOX_BOTTOM, BOX_TOP, PALETTE_HEADER
from .enums import Chooser, Woke
from .format import marks_of, window_marks

if TYPE_CHECKING:
    from prompt_toolkit.layout.controls import BufferControl, NotImplementedOrNone

    from pymux.ids import PaneId
    from pymux.main import Pymux
    from pymux.session import Session

    from .layout import LayoutManager

__all__ = ["ChooserView"]


def option_value_of(pymux: Pymux, name: str) -> str:
    """
    What one option holds, spelled the way set-option with no value
    reads it back. A window option reads the window that is active, a
    client option the client this command means.
    """
    from pymux.commands.common import option_as_written
    from pymux.options import Scope

    for table in pymux.option_tables.values():
        option = table.get(name)
        if option is not None:
            target = pymux.current_session.options if option.scope is Scope.SESSION else None
            return option_as_written(pymux, option, argparse.Namespace(g=False), target)
    return ""


def option_command_of(name: str) -> str:
    """
    The command that sets one option by name. A session option is
    `set-option`, a window option `set-window-option`, and a client
    option `set-client-option`; each scope has its own command, so a
    chooser that lists all three has to carry a name to the one that
    takes it. Lillecarl/pymux#472.
    """
    from pymux.options import ALL_CLIENT_OPTIONS, ALL_WINDOW_OPTIONS

    if name in ALL_CLIENT_OPTIONS:
        return "set-client-option"
    if name in ALL_WINDOW_OPTIONS:
        return "set-window-option"
    return "set-option"


#: What the box of each kind of chooser says it is.
_BOX_TITLES = {
    Chooser.BUFFER: "Choose a buffer",
    Chooser.OPTIONS: "Customize",
    Chooser.NOTIFICATIONS: "Notifications",
    Chooser.JOB: "Jobs",
}


class ChooserView:
    "The choosers of one client, and the windows that draw them."

    def __init__(self, layout: LayoutManager) -> None:
        self.layout = layout
        self.pymux = layout.pymux
        self.client_state = layout.client_state

        # The windows of the session, to choose from. Built once, like
        # the boxes of `LayoutManager`: a window built fresh each frame
        # is one the layout never focused.
        # Lillecarl/pymux#295.
        self._chooser: Container | None = None
        self._chooser_rows: Window | None = None
        self._chooser_search: Container | None = None

        #: The window chooser is not the box: it is a bar across the
        #: top, over the window it is switching to.
        #: Lillecarl/pymux#327.
        self._bar: Container | None = None
        self._bar_rows: Window | None = None
        self._bar_search: Container | None = None

    def every_window(self) -> list[arrangement.Window]:
        """
        The windows of every session of this server, in session order.

        The chooser lists them all, so a person picks a window without
        first having to name the session it is in. tmux's choose-tree
        does the same thing with a tree. Lillecarl/pymux#323.
        """
        return [window for session in self.pymux.sessions for window in session.arrangement.windows]

    def display_chooser(self, template: str = "") -> None:
        """
        Show the windows of every session, to choose from.

        It opens with the chooser on the window this client looks
        at, the way tmux's tree opens on the current one. A template
        is the tmux `%%`: the command to run on the window the
        chooser points at, instead of switching to it.
        Lillecarl/pymux#295.

        **There is no picture of a window here.** Moving the point
        switches this client to the window it lands on, so what a
        person reads under the bar is that window itself, running, at
        full size. Escape puts the client back where it started.
        Lillecarl/pymux#327.
        """
        state = self.client_state
        windows = self.every_window()
        active = state.session.arrangement.get_active_window()

        state.chooser_return_to = (state.session, active)
        state.display_popup = False
        state.chooser = Chooser.WINDOW
        state.menu_entries = []
        state.choose_window_command = template
        state.choose_window_filter.reset()
        state.choose_window_index = windows.index(active) if active in windows else 0
        state.choose_window_along = None
        self._window_bar()
        assert self._bar_rows is not None
        get_app().layout.focus(self._bar_rows)

    def _bar_width(self) -> int:
        "The columns the bar flows its entries across."
        return max(1, self.layout.room_this_client_has.columns)

    def chooser_entries(self) -> list[str]:
        """
        What the bar says about each window it lists, in its order.

        `index:name`, and `session:index:name` when the server holds
        more than one session and they flow together -- with one the
        word is the same on every entry and says nothing, and with
        `chooser-session-lines` the line's label says it once.
        Lillecarl/pymux#765. A star marks the window this client was
        on when the chooser opened, which is the one Escape goes back
        to. The window's marks come before the star, as in the status
        line, and the point clears them when it lands, as a visit does.

        The pane count is not here. The bar sits over the window
        itself, so a person counts the panes by looking at them.
        Lillecarl/pymux#327.
        """
        prefixed = len(self.pymux.sessions) > 1 and not self._chooser_grouped()
        going_back = self.client_state.chooser_return_to
        started_on = going_back[1] if going_back is not None else None

        entries = []
        for window in self.chooser_matches():
            if prefixed:
                label = "%s:%s:%s" % (
                    self._session_name_of(window),
                    window.index,
                    window.name,
                )
            else:
                label = "%s:%s" % (window.index, window.name)
            session = self.pymux.session_of_window(window)
            if session is not None:
                label += window_marks(session.options, window)
            if window is started_on:
                label += "*"
            entries.append(label)
        return entries

    def chooser_lines(self, width: int) -> list[list[int]]:
        """
        The entries packed into lines, as places in the list.

        They flow like words: an entry that does not fit starts the
        next line. One wider than the whole bar takes a line of its own
        and the screen cuts it, which is what a long word does too.
        Lillecarl/pymux#327.

        With `chooser-session-lines` on and more than one session, each
        session also starts a line, so the sessions read as groups.
        Lillecarl/pymux#531. The label column takes its width from
        every line, so the entries flow in what is left.
        """
        lines: list[list[int]] = [[]]
        width = max(1, width - self._chooser_label_width())
        room = width

        sessions: list[Session | None] = []
        if self._chooser_grouped():
            sessions = [self.pymux.session_of_window(window) for window in self.chooser_matches()]

        for index, label in enumerate(self.chooser_entries()):
            cost = len(label) + (1 if lines[-1] else 0)
            new_session = bool(sessions) and index > 0 and sessions[index] is not sessions[index - 1]
            if lines[-1] and (cost > room or new_session):
                lines.append([])
                room = width
                cost = len(label)
            lines[-1].append(index)
            room -= cost

        return lines

    def _chooser_grouped(self) -> bool:
        "Whether each session of the window chooser has lines of its own."
        return self.client_state.session.options.chooser_session_lines and len(self.pymux.sessions) > 1

    def _chooser_label_width(self) -> int:
        "The columns the session labels take, a space after them included."
        if not self._chooser_grouped():
            return 0
        sessions = {self.pymux.session_of_window(window) for window in self.chooser_matches()}
        return max((len(self._session_label(session)) for session in sessions if session), default=0) + 1

    def _session_label(self, session: Session) -> str:
        """
        A session's name, and the marks its windows carry, so a line
        says which session wants attention. Lillecarl/pymux#767.
        """
        return session.name + marks_of(session.options, session.arrangement.windows)

    def chooser_line_labels(self, lines: list[list[int]]) -> list[str]:
        """
        The session label of each line, padded to one column.

        The first line of a session names it, and a line it wrapped
        onto stays blank, so the name is said once and the entries
        still line up. Lillecarl/pymux#765.
        """
        width = self._chooser_label_width()
        if not width:
            return [""] * len(lines)
        matches = self.chooser_matches()
        labels = []
        previous = None
        for line in lines:
            session = self.pymux.session_of_window(matches[line[0]]) if line else None
            name = self._session_label(session) if session is not None and session is not previous else ""
            labels.append(name.ljust(width))
            previous = session
        return labels

    def step_line(self, step: int) -> None:
        """
        Move a line up or down, keeping the place along the line.

        A line holds as many entries as fitted on it, so the place is
        what j and k keep: the third entry of this line lands on the
        third of the next, or on its last when it is shorter. The aim
        stays the third, so the line after that lands on its third again.
        """
        state = self.client_state
        lines = self.chooser_lines(self._bar_width())
        here = state.choose_window_index

        for number, line in enumerate(lines):  # noqa: B007 -- the index leaves the loop
            if here in line:
                break
        else:
            return

        target = number + step
        if not 0 <= target < len(lines):
            return

        along = line.index(here) if state.choose_window_along is None else state.choose_window_along
        landing = lines[target]
        self.point_at(landing[min(along, len(landing) - 1)], along=along)

    def point_at(self, index: int, along: int | None = None) -> None:
        """
        Put the point on one row, and switch this client to it.

        One row past the last is the first, and one before the first
        is the last, so walking the entries cycles. The switch is the
        preview. It is the same call `select-window` makes, so
        everything a window has -- its panes, its size, its title
        bars -- is what a person sees while they choose.
        Lillecarl/pymux#327.

        `along` is the aim `step_line` keeps; every other move passes
        none and so forgets it.
        """
        state = self.client_state
        state.choose_window_along = along
        matches = self.chooser_matches()
        if not matches:
            state.choose_window_index = 0
            return

        state.choose_window_index = index % len(matches)

        if state.chooser is not Chooser.WINDOW:
            return

        self.show_window(matches[state.choose_window_index])

    def show_window(self, window: arrangement.Window) -> None:
        "Move this client onto a window, wherever that window is."
        session = self.pymux.session_of_window(window)
        if session is None:
            return

        self.pymux.attach_client_to(self.client_state, session)
        session.arrangement.set_active_window(window)
        self.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)

    def leave_chooser(self, restore: bool) -> None:
        """
        Close the chooser. `restore` puts the client back where it was
        when the chooser opened, which is what Escape means.
        """
        state = self.client_state
        going_back = state.chooser_return_to

        state.chooser = None
        state.chooser_return_to = None

        if restore and going_back is not None:
            session, window = going_back
            if window in session.arrangement.windows:
                self.show_window(window)

    def display_box_chooser(self, kind: Chooser) -> None:
        """
        Show a box of rows to choose from, of one kind:

        - `BUFFER`, the named buffers. Enter makes the chosen one the
          session's one buffer, which is what `paste-buffer` pastes --
          the thing tmux's choose-buffer marks current.
          Lillecarl/pymux#304.
        - `OPTIONS`, the options of every scope: what `customize-mode`
          is here. Enter asks for a value on the prompt, with the
          option named and what it holds as the default.
          Lillecarl/pymux#297.
        - `NOTIFICATIONS`, what the hub keeps, newest first. Enter
          takes this client to the pane one came from.
        - `JOB`, the jobs of this server, newest first. Enter shows the
          pointed job in this session's overlay, read-only; `o` opens
          it as a pane in a new window, and `t` runs its command again
          in a new window. Lillecarl/pymux#528.

        Escape closes, and there is nothing to put back: a box
        previews nothing.
        """
        assert kind is not Chooser.WINDOW
        state = self.client_state
        state.display_popup = False
        state.chooser = kind
        state.menu_entries = []
        state.choose_window_command = ""
        state.choose_window_filter.reset()
        state.choose_window_index = 0
        get_app().layout.focus(self.chooser_rows_control())

    def box_chooser_shows(self) -> bool:
        "Whether a chooser of the box kind shows, and not the bar."
        return self.client_state.chooser not in (None, Chooser.WINDOW)

    def chooser_matches(self) -> list[Any]:
        """
        The rows the chooser lists. The search narrows them: a row
        stays when what was typed is in its name or in its index,
        without case. Lillecarl/pymux#295.
        """
        text = self.client_state.choose_window_filter.text.lower()
        kind = self.client_state.chooser
        if kind is Chooser.OPTIONS:
            names = sorted(name for table in self.pymux.option_tables.values() for name in table)
            if not text:
                return names
            return [name for name in names if text in name.lower() or text in option_value_of(self.pymux, name).lower()]
        if kind is Chooser.NOTIFICATIONS:
            from pymux.hub import rows_of

            return rows_of(self.pymux, self.pymux.notification_center.notifications(), text)
        if kind is Chooser.JOB:
            from pymux.jobs import describe

            jobs = list(reversed(self.pymux.jobs.listing()))
            if not text:
                return jobs
            return [job for job in jobs if text in describe(job).lower()]
        if kind is Chooser.BUFFER:
            buffers = self.pymux.named_buffers
            if not text:
                return sorted(buffers)
            return [name for name in sorted(buffers) if text in name.lower() or text in str(len(buffers[name]))]
        windows = self.every_window()
        if not text:
            return windows
        return [
            w
            for w in windows
            if text in w.name.lower() or text in str(w.index) or text in self._session_name_of(w).lower()
        ]

    def _pointed_index(self, count: int) -> int:
        "The row the point is on, among `count`, kept inside the list."
        return min(self.client_state.choose_window_index, count - 1)

    def pointed(self):
        "The row the chooser points at, or None for an empty list."
        matches = self.chooser_matches()
        return matches[self._pointed_index(len(matches))] if matches else None

    def choose_pointed(self) -> None:
        "Take the row the chooser points at, by the kind that shows."
        chooser = self.client_state.chooser or Chooser.WINDOW
        with started_by(Origin.CHOOSER):
            note(self.pymux, Kind.CHOICE, chooser.replace("_", "-"))
            match chooser:
                case Chooser.OPTIONS:
                    self.choose_pointed_option()
                case Chooser.BUFFER:
                    self.choose_pointed_buffer()
                case Chooser.NOTIFICATIONS:
                    self.choose_pointed_notification()
                case Chooser.JOB:
                    self.choose_pointed_job()
                case _:
                    self.choose_pointed_window()

    def _session_name_of(self, window: arrangement.Window) -> str:
        "The name of the session that holds a window, or nothing."
        session = self.pymux.session_of_window(window)
        return session.name if session is not None else ""

    def choose_pointed_buffer(self) -> None:
        """
        Make the buffer the chooser points at the session's one
        buffer, which is what `paste-buffer` pastes. The chooser
        closes. Lillecarl/pymux#304.
        """
        name = self.pointed()
        self.client_state.chooser = None
        if name is None:
            return
        text = self.pymux.named_buffers[name]
        self.pymux.clipboard.set_data(ClipboardData(text))
        self.pymux.invalidate(Woke.CLICK_CHOSE_A_BUFFER)

    def choose_pointed_window(self) -> None:
        """
        Take the window the chooser points at.

        This client is on it already -- moving the point is what put it
        there -- so Enter only stops the chooser from putting it back.
        A template is the one case that undoes the switch: the command
        is what the person asked for, and being moved as well is not.
        Lillecarl/pymux#295. Lillecarl/pymux#327.
        """
        window = self.pointed()
        if window is None:
            self.leave_chooser(restore=True)
            return

        session = self.pymux.session_of_window(window)
        template = self.client_state.choose_window_command

        if template and session is not None:
            self.leave_chooser(restore=True)
            self.pymux.handle_command(template.replace("%%", "%s:%i" % (session.name, window.index)))
            return

        self.leave_chooser(restore=False)

    def choose_pointed_option(self) -> None:
        """
        Ask what the option the chooser points at should hold, on the
        prompt, with the command named and what it holds as the
        default answer. The command is the option's own scope's --
        `set-option`, `set-window-option` or `set-client-option` -- so
        a client option reaches the client this chooser is drawn on.
        The chooser closes. Lillecarl/pymux#297. Lillecarl/pymux#472.
        """
        name = self.pointed()
        self.client_state.chooser = None
        if name is None:
            return
        command = option_command_of(name)
        self.pymux.handle_command(
            "command-prompt -p '%s %s' -I '%s' '%s %s %%'"
            % (command, name, option_value_of(self.pymux, name), command, name)
        )

    def choose_pointed_notification(self) -> None:
        """
        Take this client to the pane the pointed row came from: a
        message's own, or a heading's newest. The hub closes either
        way; a pane that is gone, or a notification no pane sent,
        leaves a message saying so instead of moving.
        """
        from pymux.hub import Heading

        row = self.pointed()
        self.client_state.chooser = None
        if row is None:
            return
        record = row.newest if isinstance(row, Heading) else row.record
        found = self.pymux.window_of_pane(record.pane_id)
        if found is None:
            self.client_state.message = "The pane the notification came from is gone."
            return
        _session, window, pane = found
        self.show_window(window)
        window.active_pane = pane
        self.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)

    def pointed_job(self):
        """
        The job the chooser points at, or None for an empty list.

        The chooser lists newest first, so the point opens at the
        latest job without moving.
        """
        if self.client_state.chooser is not Chooser.JOB:
            return None
        return self.pointed()

    def choose_pointed_job(self) -> None:
        """
        Show the pointed job in this session's overlay: the tail and
        the follow, read-only. The picker closes; Escape closes the
        viewer, which never touches the job. Lillecarl/pymux#528.
        """
        job = self.pointed_job()
        self.client_state.chooser = None
        if job is None:
            return
        self.pymux.spawn_command(self.pymux.display_job_overlay(job, self.client_state.session))

    def open_pointed_job_in_pane(self) -> None:
        """
        Open the pointed job as a pane in a new window: a viewer,
        read-only, that follows the job. Closing the window never
        touches the job. Lillecarl/pymux#528.
        """
        job = self.pointed_job()
        self.client_state.chooser = None
        if job is None:
            return
        self.pymux.spawn_command(self.pymux.open_job_in_pane(job, self.client_state.session))

    def take_over_pointed_job(self) -> None:
        """
        Run the pointed job's command again, interactively, in a new
        window. The recorded job is untouched. Lillecarl/pymux#528.
        """
        job = self.pointed_job()
        self.client_state.chooser = None
        if job is None:
            return
        self.pymux.spawn_command(self.pymux.take_over_job(job, self.client_state.session))

    def _choose_job_tokens(self) -> StyleAndTextTuples:
        """
        The jobs, newest first, one `list-jobs` line each. The row
        the chooser points at carries the gutter arrow and stands
        out, and a row answers a click by showing its job in the
        overlay, the way Enter does.
        """
        from pymux.jobs import describe

        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No jobs. ")]

        chosen = self._pointed_index(len(matches))
        tokens: StyleAndTextTuples = []
        for i, job in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            tokens.append(
                (
                    style,
                    "%s%s\n" % ("> " if i == chosen else "  ", describe(job)),
                    self._create_chooser_click_handler(i),
                )
            )
        return tokens

    def _chooser_box(self) -> Container:
        """
        What a chooser lists, in a box: the windows, the named
        buffers or the options, by the kind that is showing. One box
        for the three of them, because they are one thing: the
        title, the rows and the search are shared, and only what the
        rows say and what taking a row does differ.
        Lillecarl/pymux#295. Lillecarl/pymux#304. Lillecarl/pymux#297.

        Built once, for the reason `_command_line_window` gives.
        """
        if self._chooser is not None:
            return self._chooser

        rows = Window(
            content=FormattedTextControl(
                self._chooser_tokens,
                focusable=True,
                show_cursor=False,
                get_cursor_position=lambda: Point(0, self.client_state.choose_window_index),
            ),
            height=lambda: D(min=1, max=self._chooser_room()),
            style="class:commandpalette",
        )

        search = VSplit(
            [
                Window(
                    width=2,
                    height=1,
                    content=FormattedTextControl([("class:chooser.hint", "/ ")]),
                    style="class:commandpalette",
                ),
                Window(
                    content=BufferControl(buffer=self.client_state.choose_window_filter),
                    height=1,
                    style="class:commandpalette",
                ),
            ]
        )

        def get_title() -> StyleAndTextTuples:
            return [("class:commandpalette.title", " %s " % self._chooser_title())]

        self._chooser = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(get_title),
                    style="class:commandpalette.titlebar",
                ),
                rows,
                search,
            ],
        )
        self._chooser_rows = rows
        self._chooser_search = search
        return self._chooser

    def _window_bar(self) -> Container:
        """
        The window chooser: a bar across the top of the screen.

        Not a box, and nothing of the window is hidden by more of it
        than it needs: the windows flow across the bar and wrap, and
        what is under it is the window the bar points at, running.
        Lillecarl/pymux#327.

        Built once, for the reason `_command_line_window` gives.
        """
        if self._bar is not None:
            return self._bar

        rows = Window(
            content=FormattedTextControl(
                self._choose_window_tokens,
                focusable=True,
                show_cursor=False,
            ),
            height=lambda: D.exact(len(self.chooser_lines(self._bar_width()))),
            wrap_lines=False,
            style="class:commandpalette",
        )

        search = VSplit(
            [
                Window(
                    width=2,
                    height=1,
                    content=FormattedTextControl([("class:chooser.hint", "/ ")]),
                    style="class:commandpalette",
                ),
                Window(
                    content=BufferControl(buffer=self.client_state.choose_window_filter),
                    height=1,
                    style="class:commandpalette",
                ),
            ]
        )

        self._bar = HSplit([rows, search])
        self._bar_rows = rows
        self._bar_search = search
        return self._bar

    def chooser_rows_control(self) -> Window:
        "Where the keys of the chooser that shows belong."
        if self.client_state.chooser is Chooser.WINDOW:
            self._window_bar()
            rows = self._bar_rows
        else:
            self._chooser_box()
            rows = self._chooser_rows
        assert rows is not None
        return rows

    def chooser_search_control(self) -> Container:
        "The search line of the chooser that shows."
        if self.client_state.chooser is Chooser.WINDOW:
            self._window_bar()
            search = self._bar_search
        else:
            self._chooser_box()
            search = self._chooser_search
        assert search is not None
        return search

    def _chooser_room(self) -> int:
        "The rows the box has for its list."
        rows = self.layout.room_this_client_has.rows
        return max(1, rows - BOX_TOP - BOX_BOTTOM - PALETTE_HEADER)

    def _chooser_title(self) -> str:
        "What the box of the chooser says it is."
        chooser = self.client_state.chooser
        return "" if chooser is None else _BOX_TITLES.get(chooser, "")

    def _chooser_tokens(self) -> StyleAndTextTuples:
        """
        The rows of the box, by the kind that shows.

        The windows are not here: they flow across a bar of their own.
        Lillecarl/pymux#327.
        """
        match self.client_state.chooser:
            case Chooser.OPTIONS:
                return self._choose_options_tokens()
            case Chooser.NOTIFICATIONS:
                return self._choose_notification_tokens()
            case Chooser.JOB:
                return self._choose_job_tokens()
            case _:
                return self._choose_buffer_tokens()

    def _choose_options_tokens(self) -> StyleAndTextTuples:
        """
        The options, one row each: the name, and what it holds. A
        window or client option says which scope it is, because the
        three scopes are set by three commands; a server option says
        so too, because it is one value for every session.
        Lillecarl/pymux#297. Lillecarl/pymux#472.
        """
        from pymux.options import ALL_CLIENT_OPTIONS, ALL_SERVER_OPTIONS, ALL_WINDOW_OPTIONS

        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No option matches. ")]

        chosen = self._pointed_index(len(matches))
        tokens: StyleAndTextTuples = []
        for i, name in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            value = option_value_of(self.pymux, name)
            if name in ALL_WINDOW_OPTIONS:
                kind = " (window)"
            elif name in ALL_CLIENT_OPTIONS:
                kind = " (client)"
            elif name in ALL_SERVER_OPTIONS:
                kind = " (server)"
            else:
                kind = ""
            tokens.append(
                (
                    style,
                    "%s%s = %s%s\n" % ("> " if i == chosen else "  ", name, value, kind),
                    self._create_chooser_click_handler(i),
                )
            )
        return tokens

    def _choose_notification_tokens(self) -> StyleAndTextTuples:
        """
        The hub: a heading for each window that sent something, newest
        first, and its messages under it. A dot says unread, which is
        the window's notification mark. A heading says the window, the
        program and the directory its shell reported; a message says
        what it said, how often in a row, and what it came through.
        A row answers a click by jumping to its pane.
        Lillecarl/pymux#768.
        """
        from pymux.hub import Heading, age, heading_text

        rows = self.chooser_matches()
        if not rows:
            return [("class:chooser.hint", " No notifications. ")]

        now = time.time()
        chosen = self._pointed_index(len(rows))
        tokens: StyleAndTextTuples = []
        for i, row in enumerate(rows):
            pointed = i == chosen
            gutter = "> " if pointed else "  "
            dot = "\u25cf " if row.unread else "  "
            handler = self._create_chooser_click_handler(i)
            if isinstance(row, Heading):
                text = heading_text(row)
                where = self._where_pane_is(row.newest.pane_id)
                if where:
                    text += " " + where
                style = "class:chooser.selected" if pointed else "class:chooser.session"
                tokens.append((style, "%s%s%s  %s\n" % (gutter, dot, text, age(now - row.newest.at)), handler))
                continue
            record = row.record
            text = record.title
            if record.body:
                text += " - " + record.body if text else record.body
            if row.count > 1:
                text += " \u00d7%d" % row.count
            style = "class:chooser.selected" if pointed else "class:commandpalette"
            tokens.append((style, "%s%s  %s" % (gutter, dot, text[:120]), handler))
            if record.source:
                tokens.append((style if pointed else "class:chooser.hint", "  " + record.source, handler))
            tokens.append((style, "\n", handler))
        return tokens

    def _where_pane_is(self, pane_id: PaneId | None) -> str:
        """
        The directory the shell of that pane reported, with the home
        directory as `~`, or nothing. Lillecarl/pymux#498.

        **Only what the shell said**, not the process table that
        `#{pane_current_path}` falls back to: that is a system call per
        row per frame, and over ssh it names the local ssh.
        """
        pane = self.pymux.panes_by_id.get(pane_id) if pane_id is not None else None
        directory = pane.current_directory if pane is not None else None
        if not directory:
            return ""
        home = os.path.expanduser("~")
        if directory == home or directory.startswith(home + os.sep):
            return "~" + directory[len(home) :]
        return directory

    def _choose_buffer_tokens(self) -> StyleAndTextTuples:
        """
        The named buffers, one row each: the name, and how much it
        holds. The row the chooser points at carries the gutter arrow
        and stands out, and a row answers a click the way a window's
        row does. Lillecarl/pymux#304.
        """
        matches = self.chooser_matches()
        if not matches:
            return [("class:chooser.hint", " No buffer matches. ")]

        chosen = self._pointed_index(len(matches))
        tokens: StyleAndTextTuples = []
        for i, name in enumerate(matches):
            style = "class:chooser.selected" if i == chosen else "class:commandpalette"
            tokens.append(
                (
                    style,
                    "%s%-24s %i\n"
                    % (
                        "> " if i == chosen else "  ",
                        name,
                        len(self.pymux.named_buffers[name]),
                    ),
                    self._create_chooser_click_handler(i),
                )
            )
        return tokens

    def _choose_window_tokens(self) -> StyleAndTextTuples:
        """
        The windows of every session, flowing across the bar.

        Like words on a line: they sit side by side and wrap when the
        line is full. A row each was right in a box; a bar is one to
        three rows over the window a person is reading, and a row each
        would cover it. Lillecarl/pymux#327.

        The entry the bar points at stands out, and a star marks the
        one Escape goes back to. An entry answers a click, the way a
        column of the strip does.
        """
        entries = self.chooser_entries()
        if not entries:
            return [("class:chooser.hint", " No window matches. ")]

        chosen = self._pointed_index(len(entries))

        tokens: StyleAndTextTuples = []
        lines = self.chooser_lines(self._bar_width())
        for number, (line, label) in enumerate(zip(lines, self.chooser_line_labels(lines), strict=True)):
            if number:
                tokens.append(("class:commandpalette", "\n"))
            if label:
                tokens.append(("class:chooser.session", label))
            for place, index in enumerate(line):
                if place:
                    tokens.append(("class:commandpalette", " "))
                tokens.append(
                    (
                        "class:chooser.selected" if index == chosen else "class:commandpalette",
                        entries[index],
                        self._create_chooser_click_handler(index),
                    )
                )
        return tokens

    def _create_chooser_click_handler(self, row: int) -> Callable[[MouseEvent], NotImplementedOrNone]:
        """
        Return a mouse handler that takes the row: the window it
        names, the buffer or option it names, the notification it
        came from, or the job it names, by the kind that shows.
        """

        def handler(mouse_event: MouseEvent) -> NotImplementedOrNone:
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                with started_by(Origin.MOUSE):
                    if self.client_state.chooser is Chooser.WINDOW:
                        # Through `point_at`, so the click switches to the
                        # window before taking it -- the same path the keys
                        # take. Lillecarl/pymux#327.
                        self.point_at(row)
                    else:
                        self.client_state.choose_window_index = row
                    self.choose_pointed()
                return None
            return NotImplemented

        return handler
