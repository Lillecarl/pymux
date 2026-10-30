"""
The menu of `display-menu`: a box of entries, each with the key that
runs it. What it holds lives on `ClientState`; this draws it and runs
the entry a key or a click picks. Lillecarl/pymux#297.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import (
    Container,
    HSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

from ..keys.actions import Origin, started_by

if TYPE_CHECKING:
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout.controls import NotImplementedOrNone

    from .layout import LayoutManager

__all__ = ["Menu"]


class Menu:
    "The menu of one client, and the window that draws it."

    def __init__(self, layout: LayoutManager) -> None:
        self.layout = layout
        self.pymux = layout.pymux
        self.client_state = layout.client_state

        self._box: Container | None = None
        self._rows: Window | None = None

    def show(self, entries: list[tuple[str, str, str]], title: str = "") -> None:
        """
        The menu of `display-menu`: a line per entry, with the key
        that runs it beside the name. The box draws where the other
        boxes draw; tmux takes a position, and -x and -y are accepted
        for it and change nothing. Lillecarl/pymux#297.
        """
        self.client_state.display_popup = False
        self.client_state.chooser = None
        self.client_state.menu_entries = entries
        self.client_state.menu_title = title
        self.box()
        assert self._rows is not None
        get_app().layout.focus(self._rows)

    def close(self) -> None:
        "The menu goes, without running anything."
        self.client_state.menu_entries = []

    def key_pressed(self, key: Keys | str, data: str | None = None) -> None:
        """
        A key while the menu shows: the entry whose key it is runs,
        and a key that is nobody's stays in the menu, which is modal.
        The spelling of a named key is tmux's -- Enter, Space, Tab.
        """
        if isinstance(key, str) and key in ("<enter>", "<space>", "<tab>"):
            spelling = {"<enter>": "Enter", "<space>": "Space", "<tab>": "Tab"}[key]
        elif data:
            spelling = data
        else:
            spelling = key if isinstance(key, str) else str(key)

        for entry in self.client_state.menu_entries:
            if entry[0].lower() == spelling.lower():
                self._run_entry(entry)
                return

    def _run_entry(self, entry: tuple[str, str, str]) -> None:
        _key, _name, command = entry
        self.close()
        with started_by(Origin.MENU):
            self.pymux.handle_command(command)

    def _click_handler(self, entry: tuple[str, str, str]) -> Callable[[MouseEvent], NotImplementedOrNone]:
        "Return a mouse handler that runs the entry when clicking."

        def handler(mouse_event: MouseEvent) -> NotImplementedOrNone:
            if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                with started_by(Origin.MOUSE):
                    self._run_entry(entry)
                return None
            return NotImplemented  # Event not handled here.

        return handler

    def _tokens(self) -> StyleAndTextTuples:
        """
        The entries of the menu, one row each: the key that runs it,
        and its name. A row answers a click the way a chooser's row
        does. Lillecarl/pymux#297.
        """
        result: StyleAndTextTuples = []
        for key, name, _command in self.client_state.menu_entries:
            handler = self._click_handler((key, name, _command))
            result.append(("class:chooser.hint", " %s " % (key or " "), handler))
            result.append(("", "%s\n" % name, handler))
        return result

    def box(self) -> Container:
        """
        The menu, in a box. Built once, for the reason
        `LayoutManager._command_line_window` gives; the title is asked each frame,
        because the command that opened the menu names it.
        Lillecarl/pymux#297.
        """
        if self._box is not None:
            return self._box

        self._rows = Window(
            content=FormattedTextControl(
                self._tokens,
                focusable=True,
                show_cursor=False,
            ),
            style="class:commandpalette",
        )

        def get_title() -> StyleAndTextTuples:
            return [
                (
                    "class:commandpalette.title",
                    " %s " % (self.client_state.menu_title or "Menu"),
                )
            ]

        self._box = HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(get_title),
                    style="class:commandpalette.titlebar",
                ),
                self._rows,
            ],
        )
        return self._box
