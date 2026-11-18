"""
The status line of one client: `status-left`, the list of windows and
`status-right`, and how many rows the list takes. Lillecarl/pyterm#777.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, override

from prompt_toolkit.application.current import set_app
from prompt_toolkit.formatted_text import AnyFormattedText, StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.containers import (
    WindowAlign,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.utils import get_cwidth

from pymux.core import window_tree

from ..enums import Justify, Woke
from ..format import format_pymux_string
from ..keys.actions import Kind, Origin, note, started_by
from ..keys.mouse_keys import MouseLocation

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone

    from .layout import LayoutManager

__all__ = ["PASSTHROUGH_MARK", "StatusLine", "StatusPart"]

#: tmux's `mouse_status_range` for each place on the status line.
_RANGES = {
    MouseLocation.STATUS: "window",
    MouseLocation.STATUS_LEFT: "left",
    MouseLocation.STATUS_RIGHT: "right",
}


class _SelectWindow:
    "A window's entry in the window list: a press selects it."

    def __init__(self, status: StatusLine, window: window_tree.Window) -> None:
        self.status = status
        self.window = window

    def __call__(self, mouse_event: MouseEvent) -> NotImplementedOrNone:
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self.status.client_state.session.arrangement.set_active_window(self.window)
            self.status.pymux.invalidate(Woke.CLICK_CHOSE_A_WINDOW)
            with started_by(Origin.MOUSE):
                note(self.status.pymux, Kind.CLICK, "select-window")
            return None
        return NotImplemented  # Event not handled here.


class StatusPart(FormattedTextControl):
    """
    One part of the status line. A mouse event over it is a mouse key
    first: over a window's entry `Status`, else this part's `location`.
    Lillecarl/pyterm#1034.
    """

    def __init__(self, status: StatusLine, text: Callable[[], AnyFormattedText], location: MouseLocation) -> None:
        super().__init__(text)
        self.status = status
        self.location = location

    def _handler_at(self, mouse_event: MouseEvent) -> object:
        "The handler of the fragment under the pointer, the way `FormattedTextControl.mouse_handler` finds it."
        lines = list(split_lines(self._fragments or []))
        if mouse_event.position.y >= len(lines):
            return None
        count = 0
        for item in lines[mouse_event.position.y]:
            count += len(item[1])
            if count > mouse_event.position.x:
                return item[2] if len(item) >= 3 else None
        return None

    @override
    def mouse_handler(self, mouse_event: MouseEvent) -> NotImplementedOrNone:
        window = getattr(self._handler_at(mouse_event), "window", None)
        location = MouseLocation.STATUS if window is not None else self.location
        if self.status.pymux.key_bindings_manager.mouse(
            mouse_event,
            location,
            window=window,
            status_range=_RANGES.get(location, ""),
            replay=lambda: super(StatusPart, self).mouse_handler(mouse_event),
        ):
            return None
        return super().mouse_handler(mouse_event)


#: Before `status-right` while passthrough holds every key: a person
#: who wonders why the prefix does nothing reads the answer by the
#: clock. In code and not in the default format, so a person's own
#: `status-right` gets it too. Lillecarl/pyterm#532.
PASSTHROUGH_MARK = " PASSTHROUGH "


def _tab_style(window: window_tree.Window) -> str:
    """
    The tab colour of the window's active pane, "OSC 6", as a style to
    add, with black or white over it, whichever reads. Lillecarl/pyterm#788.
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
        #: `left` and `right`, while this client renders. The room for
        #: the window list reads both, the status line draws them, and
        #: `what_time_moves` reads them after: three formats a frame of
        #: what may be a template. Lillecarl/pyterm#826.
        self._frame_sides: dict[str, str] = {}
        #: The widest each side has been since `_held_for`, the terminal
        #: width and the options, last changed: what the room for the
        #: window list leaves to it.
        self._widest: dict[str, int] = {}
        self._held_for: tuple[object, ...] = ()

    def frame_changed(self) -> None:
        "A render of this client starts or ends: the cached lines go."
        self._frame_lines = None
        self._frame_sides = {}

    def _select_window_handler(self, window: window_tree.Window) -> _SelectWindow:
        "Return a mouse handler that selects the given window when clicking."
        return _SelectWindow(self, window)

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

        recording = self.client_state.client.recording
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
        are cut where it ends. Lillecarl/pyterm#725.

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

        Each side is as wide as its text, up to its `-length` option, as
        the `VSplit` of the status line draws it. **But the room is
        held at the widest each side has been**, for this terminal width
        and these options. A side that holds a counter, such as a frame
        rate or a byte rate, moves a cell or two on every redraw; near a
        break the window list then wrapped and unwrapped with it, and
        the panes changed size each time. Now only a resize, a changed
        option or the window list itself moves the break.
        """
        columns = self.client_state.app.output.get_size().columns
        options = self.client_state.session.options
        held_for = (columns, options.status_left, options.status_right, options.status_left_length, self.right_length())
        if held_for != self._held_for:
            self._held_for = held_for
            self._widest = {}
        left = self._held("left", min(get_cwidth(self.left()), options.status_left_length))
        right = self._held("right", min(get_cwidth(self.right()), self.right_length()))
        return max(1, columns - left - right)

    def _held(self, side: str, width: int) -> int:
        widest = self._widest[side] = max(width, self._widest.get(side, 0))
        return widest

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
        return self._side("left", self.client_state.session.options.status_left)

    def right(self) -> str:
        text = self._side("right", self.client_state.session.options.status_right)
        if self._in_passthrough():
            text = PASSTHROUGH_MARK + text
        return text

    def _side(self, side: str, string: str) -> str:
        "One side of the status line, formatted once a render."
        if self.layout.frame_plans is None:
            return self._format(string)
        found = self._frame_sides.get(side)
        if found is None:
            found = self._frame_sides[side] = self._format(string)
        return found

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
        Lillecarl/pyterm#583.
        """
        if self.client_state.session.arrangement.get_active_window() is None:
            return ""
        return format_pymux_string(
            self.pymux,
            string,
            session=self.client_state.session,
            client=self.client_state,
        )

    def part(self, text: Callable[[], AnyFormattedText], location: MouseLocation) -> StatusPart:
        "The control of one part of the status line, whose mouse events are mouse keys at `location`."
        return StatusPart(self, text, location)

    def align(self) -> WindowAlign:
        if self.client_state.session.options.status_justify == Justify.RIGHT:
            return WindowAlign.RIGHT
        if self.client_state.session.options.status_justify == Justify.CENTER:
            return WindowAlign.CENTER
        return WindowAlign.LEFT
