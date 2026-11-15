"""
A client's application, which knows why it draws. Lillecarl/pyterm#996.

A frame that only moves the clock or a meter in the status line is not
content: counting it made `client_fps` count its own redraw, and an
idle client never read zero.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from prompt_toolkit.application import Application

__all__ = ["ClientApplication"]


class ClientApplication(Application[Any]):
    """
    `Application`, told which of its frames only time asked for.

    **The cause is noted before `invalidate`'s own guard.** prompt_toolkit
    returns early while a redraw is pending, before `on_invalidate`
    fires, so a pane write that lands behind the clock's request is seen
    here and nowhere else.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._for_time = False
        self._asked_for_time = False
        self._asked_otherwise = False
        #: Whether the frame being drawn is one only time asked for.
        self.drawing_for_time = False
        #: The meter readings the last frame drew, by name: what it
        #: showed, and how to read it again (`Meters.sample`).
        self.meters_shown: dict[str, tuple[str, Callable[[], str]]] = {}

    def shows_meter(self, name: str, read: Callable[[], str]) -> str:
        "Read a meter for this frame, and note it so a change redraws it."
        text = read()
        self.meters_shown[name] = (text, read)
        return text

    def invalidate_for_time(self) -> None:
        "Ask for a frame because the clock or a meter moved, and nothing else."
        self._for_time = True
        try:
            self.invalidate()
        finally:
            self._for_time = False

    @override
    def invalidate(self) -> None:
        if self._for_time:
            self._asked_for_time = True
        else:
            self._asked_otherwise = True
        super().invalidate()

    @override
    def _redraw(self, render_as_done: bool = False) -> None:
        # A redraw nobody asked for, such as a resize, is content.
        self.drawing_for_time = self._asked_for_time and not self._asked_otherwise
        self._asked_for_time = self._asked_otherwise = False
        self.meters_shown = {}
        super()._redraw(render_as_done)
