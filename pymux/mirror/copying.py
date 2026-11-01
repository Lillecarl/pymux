"""
Copy mode of a mirrored pane, run on the owner for the peer that asked.

A peer holds no history, so a person there reads it here. This server
runs a copy-mode application of its own over the pane, draws it into a
screen of the pane's size, and the mirror sends frames of that screen
in place of the pane's (`MirrorSource.frame`). The keys and the mouse
of the person at the peer go to that application while it runs. Each
peer has a copy mode of its own, as each client has.
Lillecarl/pymux#759.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import Input
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.selection import SelectionType
from prompt_toolkit.styles import DynamicStyle
from ptterm.copy_mode import copy_mode_key_bindings
from pyte.mouse import MouseAction
from pyte.screen import Screen
from pyte.streams import Stream

from ..connection.client_io import ClientInput
from ..enums import EditingKeys
from ..log import logger
from ..style.style import DEFAULT_THEME, theme

if TYPE_CHECKING:
    from ptterm import Terminal

    from .source import MirrorSource

__all__ = ["MirrorCopy", "Place", "place_of"]

#: A caret, where the selection starts and its kind (`CopyView.place`).
type Place = tuple[int, int | None, str | None]

_KINDS = frozenset(kind.value for kind in SelectionType)


def place_of(value: object) -> Place | None:
    "The `Place` a packet carries under `copying`, or None for anything else."
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    caret, start, kind = value
    if type(caret) is not int or caret < 0:
        return None
    if start is None or kind is None:
        return caret, None, None
    if type(start) is not int or start < 0 or kind not in _KINDS:
        return caret, None, None
    return caret, start, kind


#: The button of an SGR mouse report for each action a peer sends.
_SGR_BUTTONS = {
    MouseAction.PRESS: (0, "M"),
    MouseAction.RELEASE: (0, "m"),
    MouseAction.WHEEL_UP: (64, "M"),
    MouseAction.WHEEL_DOWN: (65, "M"),
}


class _Drawn:
    "The stdout of the copy application: each frame it flushes is fed to the screen the peer is sent."

    def __init__(self, copy: MirrorCopy) -> None:
        self.copy = copy
        self._buffer: list[str] = []

    def write(self, data: str) -> int:
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        data = "".join(self._buffer)
        self._buffer = []
        if data:
            self.copy.drew(data)

    def isatty(self) -> bool:
        return True


class MirrorCopy:
    "One peer's copy mode over one pane of this server."

    def __init__(self, source: MirrorSource, terminal: Terminal, at: Place | None = None) -> None:
        self.source = source
        self.terminal = terminal
        #: Where the peer was in copy mode before it linked again (`CopyView.place`).
        self.at = at
        page = source.control.screen
        self.screen = Screen(page.lines, page.columns, write_process_input=lambda data: None)
        self._stream = Stream(self.screen)
        self._input = ClientInput(send_packet=lambda packet: None)
        self.app: Application[None] = Application(
            input=cast(Input, self._input),
            output=Vt100_Output(
                _Drawn(self),  # type: ignore[arg-type]
                self._size,
                term="xterm-256color",
                default_color_depth=ColorDepth.TRUE_COLOR,
                enable_cpr=False,
            ),
            layout=Layout(terminal.container),
            key_bindings=copy_mode_key_bindings(),
            mouse_support=True,
            full_screen=True,
            color_depth=ColorDepth.TRUE_COLOR,
            style=DynamicStyle(self._style),
        )
        self.app.key_processor.before_key_press += self._sync_editing_mode
        self.app.after_render += self._after_render
        self.ended = False

    def _size(self) -> Size:
        page = self.source.control.screen
        return Size(rows=page.lines, columns=page.columns)

    def _style(self):
        peer = self.source.link.terminal
        return theme(peer.theme if peer is not None else DEFAULT_THEME)

    def _sync_editing_mode(self, _event: object = None) -> None:
        keys = self.source.link.connection.pymux.options_of_pane(self.source.pane).mode_keys
        self.app.editing_mode = EditingMode.VI if keys is EditingKeys.VI else EditingMode.EMACS

    async def run(self, scroll: bool) -> None:
        "Run copy mode until the person leaves it or the mirror ends."
        if self.ended:
            # The mirror ended before this task started.
            self._input.close()
            self.source.copy_ended(self)
            return

        def opened() -> None:
            self._sync_editing_mode()
            view = self.terminal.enter_copy_mode()
            if view is None:
                return
            if self.at is not None:
                view.go_to(*self.at)
            elif scroll:
                view.scroll(-view.wheel_lines())

        try:
            await self.app.run_async(pre_run=opened, set_exception_handler=False, handle_sigwinch=False)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            logger.exception("Copy mode of pane %s for a peer crashed.", self.source.pane.pane_id)
        finally:
            self.end()
            self._input.close()
            self.source.copy_ended(self)

    @property
    def place(self) -> Place | None:
        "The caret and the selection, once copy mode is open."
        view = self.terminal.copy_views.of(self.app)
        return None if view is None else view.place

    def end(self) -> None:
        "Leave copy mode, from this side."
        if self.ended:
            return
        self.ended = True
        with set_app(self.app):
            self.terminal.copy_views.pop()
        future = self.app.future
        if self.app.is_running and future is not None and not future.done():
            self.app.exit()

    def _after_render(self, _app: object) -> None:
        # The person left copy mode: the terminal shows the live window now.
        if not self.ended and self.terminal.copy_views.of(self.app) is None:
            self.end()

    def drew(self, data: str) -> None:
        self._stream.feed(data)
        self.source.queue()

    def page_changed(self) -> None:
        "The pane changed under copy mode: only its size matters to a copy."
        page = self.source.control.screen
        if (page.lines, page.columns) != (self.screen.lines, self.screen.columns):
            self.screen.resize(page.lines, page.columns)
            self.app.invalidate()

    def type(self, data: str) -> None:
        "Input data as the peer's terminal sent it."
        self._input.send_text(data)

    def paste(self, text: str) -> None:
        self._input.send_text("\x1b[200~" + text + "\x1b[201~")

    def mouse(self, action: MouseAction, column: int, row: int) -> None:
        report = _SGR_BUTTONS.get(action)
        if report is None:
            return
        button, end = report
        self._input.send_text("\x1b[<%d;%d;%d%s" % (button, column + 1, row + 1, end))
