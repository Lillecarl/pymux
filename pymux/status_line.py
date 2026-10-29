"""
The status line of one client: `status-left`, the list of windows and
`status-right`, and how many rows the list takes. Lillecarl/pymux#775.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.application.current import set_app
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    WindowAlign,
)
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth

from pymux import arrangement

from .actions import Kind, Origin, note, started_by
from .enums import Justify, Woke
from .format import format_pymux_string

if TYPE_CHECKING:
    from prompt_toolkit.layout.controls import NotImplementedOrNone

    from .layout import LayoutManager

__all__ = ["PASSTHROUGH_MARK", "StatusLine"]

#: Before `status-right` while passthrough holds every key: a person
#: who wonders why the prefix does nothing reads the answer by the
#: clock. In code and not in the default format, so a person's own
#: `status-right` gets it too. Lillecarl/pymux#530.
PASSTHROUGH_MARK = " PASSTHROUGH "


def _tab_style(window: arrangement.Window) -> str:
    """
    The tab colour of the window's active pane, "OSC 6", as a style to
    add, with black or white over it, whichever reads. Lillecarl/pymux#786.
    """
    pane = window.active_pane
    color = pane.screen.tab_color if pane is not None else None
    if color is None:
        return ""
    red, green, blue = color
    ink = "#000000" if 299 * red + 587 * green + 114 * blue > 128_000 else "#ffffff"
    return " bg:#%02x%02x%02x fg:%s" % (red, green, blue, ink)


class StatusLine:
    "What the status line of one client shows."

    def __init__(self, layout: LayoutManager) -> None:
        self.layout = layout
        self.pymux = layout.pymux
        self.client_state = layout.client_state

        #: `_lines`'s answer, while this client renders. The
        #: room for the body asks it for every float and the plane
        #: asks it again, and each answer formats every window.
        self._frame_lines: list[StyleAndTextTuples] | None = None

    def frame_changed(self) -> None:
        "A render of this client starts or ends: the cached lines go."
        self._frame_lines = None

    def _select_window_handler(self, window: arrangement.Window) -> Callable[[MouseEvent], NotImplementedOrNone]:
        "Return a mouse handler that selects the given window when clicking."

        def handler(mouse_event: MouseEvent) -> NotImplementedOrNone:
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                self.client_state.session.arrangement.set_active_window(window)
                self.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)
                with started_by(Origin.MOUSE):
                    note(self.pymux, Kind.CLICK, "select-window")
                return None
            return NotImplemented  # Event not handled here.

        return handler

    def window_list(self) -> StyleAndTextTuples:
        "The tokens for the status bar: the window list, a line per row."
        result: StyleAndTextTuples = []
        for row, line in enumerate(self._lines()):
            if row > 0:
                result.append(("", "\n"))
            for i, entry in enumerate(line):
                if i > 0:
                    result.append(("", " "))
                result.append(entry)
        return result

    def _entries(self) -> StyleAndTextTuples:
        "Each thing the window list shows, one fragment each."
        result: StyleAndTextTuples = []

        recording = self.client_state.connection.recording
        if recording is not None:
            result.append(("class:recording", " ● REC %s " % recording.name))

        for w in self.client_state.session.arrangement.windows:
            if w == self.client_state.session.arrangement.get_active_window():
                style = "class:window.current"
                format_str = self.client_state.session.options.window_status_current_format

            else:
                if w.bell or w.notified:
                    style = "class:window.bell"
                elif w.activity or w.silence:
                    style = "class:window.activity"
                else:
                    style = "class:window"
                format_str = self.client_state.session.options.window_status_format

            result.append(
                (
                    style + _tab_style(w),
                    format_pymux_string(
                        self.pymux,
                        format_str,
                        window=w,
                        session=self.client_state.session,
                        client=self.client_state,
                    ),
                    self._select_window_handler(w),
                )
            )

        return result

    def _lines(self) -> list[StyleAndTextTuples]:
        """
        The window list, broken into the rows it takes.

        An entry moves to the next row whole, and never breaks across
        two. From `status-max-rows` on, the rest go on the last row and
        are cut where it ends. Lillecarl/pymux#723.

        **Under this client's application, whoever asks.** The plane
        asks it of every watcher while one of them renders, and which
        window is current -- the `*` of `#F`, and so the width of an
        entry -- is a question each client answers for itself.
        """
        if self.layout.frame_plans is not None and self._frame_lines is not None:
            return self._frame_lines

        with set_app(self.client_state.app):
            room = self._room_for_window_list()
            lines: list[StyleAndTextTuples] = [[]]
            used = 0
            for entry in self._entries():
                width = get_cwidth(entry[1])
                wanted = used + 1 + width if lines[-1] else width
                if lines[-1] and wanted > room and len(lines) < self.client_state.session.options.status_max_rows:
                    lines.append([entry])
                    used = width
                else:
                    lines[-1].append(entry)
                    used = wanted

        if self.layout.frame_plans is not None:
            self._frame_lines = lines
        return lines

    def _room_for_window_list(self) -> int:
        """
        The columns between `status-left` and `status-right`.

        The same arithmetic the `VSplit` of the status line does: each
        side is as wide as its text, up to its `-length` option.
        """
        columns = self.client_state.app.output.get_size().columns
        left = min(get_cwidth(self.left()), self.client_state.session.options.status_left_length)
        right = min(get_cwidth(self.right()), self.right_length())
        return max(1, columns - left - right)

    def rows(self) -> int:
        """
        How many rows this client's status line takes off its terminal.

        **Asked of this client, and not of the one rendering.** The
        plane asks it of every watcher, so it reads this client's own
        `full-screen` rather than `Pymux.show_status`.
        """
        if not self.client_state.session.options.status or self.client_state.options.full_screen:
            return 0
        return len(self._lines())

    def left(self) -> str:
        return self._format(self.client_state.session.options.status_left)

    def right(self) -> str:
        text = self._format(self.client_state.session.options.status_right)
        if self._in_passthrough():
            text = PASSTHROUGH_MARK + text
        return text

    def _in_passthrough(self) -> bool:
        tables = self.client_state.key_tables
        return bool(tables) and tables[-1] == "passthrough"

    def right_length(self) -> int:
        """
        The most `status-right` may take, and the room for what pymux
        puts beside it: the passthrough mark.
        """
        length = self.client_state.session.options.status_right_length
        if self._in_passthrough():
            length += len(PASSTHROUGH_MARK)
        return length

    def _format(self, string: str) -> str:
        """
        A status string, or nothing when the session holds no window to
        format it for. A session is empty before its first window and
        after its last, and a client draws through both.
        Lillecarl/pymux#581.
        """
        if self.client_state.session.arrangement.get_active_window() is None:
            return ""
        return format_pymux_string(
            self.pymux,
            string,
            session=self.client_state.session,
            client=self.client_state,
        )

    def align(self) -> WindowAlign:
        if self.client_state.session.options.status_justify == Justify.RIGHT:
            return WindowAlign.RIGHT
        if self.client_state.session.options.status_justify == Justify.CENTER:
            return WindowAlign.CENTER
        return WindowAlign.LEFT
