"""
The notifications a client shows as boxes in a corner of its screen,
newest on top. A click on one shows the pane it came from and closes
it. Lillecarl/pymux#826.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    Container,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone

    from pymux.notify.notifications import Notification

    from .layout import LayoutManager

__all__ = ["Popups"]

#: A notification box, in cells: a width that fits a title, and the
#: rows of body it shows. Lillecarl/pymux#826.
POPUP_WIDTH = 44
MIN_POPUP_WIDTH = 12
POPUP_BODY_ROWS = 3


class Popups:
    """
    The notification boxes of one client's layout.
    """

    def __init__(self, manager: LayoutManager) -> None:
        self.manager = manager

        self._box: Window | None = None

    def box(self) -> Container:
        """
        The notifications this client shows as boxes, newest on top.
        Built once, for the reason `_command_line_window` gives.
        Lillecarl/pymux#826.
        """
        if self._box is None:
            self._box = Window(
                FormattedTextControl(self.tokens, focusable=False),
                width=lambda: D.exact(self.width()),
                dont_extend_height=True,
                style="class:notification-popup",
            )
        return self._box

    def show(self, top_right: bool) -> bool:
        "Whether the boxes draw in this corner. The list goes first: it is empty on almost every frame."
        return bool(self.manager.client_state.popups) and self.manager.cursor_is_in_top_right() is not top_right

    def width(self) -> int:
        return max(MIN_POPUP_WIDTH, min(POPUP_WIDTH, self.manager.room_this_client_has.columns - 2))

    def tokens(self) -> StyleAndTextTuples:
        """
        One box per notification: the title, then up to `POPUP_BODY_ROWS`
        rows of the body. A click shows the pane it came from, as a
        click on a desktop notification does, and closes the box.
        """
        inner = self.width() - 4
        tokens: StyleAndTextTuples = []
        border = "class:notification-popup.border"
        for number, record in enumerate(reversed(self.manager.client_state.popups)):
            click = self.click(record)
            rows: list[str] = textwrap.wrap(record.body, inner)[:POPUP_BODY_ROWS]
            if number:
                tokens.append((border, "\n", click))
            tokens.append((border, "╭" + "─" * (inner + 2) + "╮\n", click))
            for style, text in [("class:notification-popup.title", record.title[:inner])] + [("", row) for row in rows]:
                tokens.append((border, "│ ", click))
                tokens.append((style, text.ljust(inner), click))
                tokens.append((border, " │\n", click))
            tokens.append((border, "╰" + "─" * (inner + 2) + "╯", click))
        return tokens

    def click(self, record: Notification) -> Callable[[MouseEvent], NotImplementedOrNone]:
        def click(mouse_event: MouseEvent) -> NotImplementedOrNone:
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return NotImplemented
            pane = self.manager.pymux.panes_by_id.get(record.pane_id) if record.pane_id is not None else None
            if pane is not None:
                self.manager.pymux.show_pane_on_click(self.manager.client_state, pane)
            self.manager.pymux.notifier.drop_popup(self.manager.client_state, record)
            return None

        return click
