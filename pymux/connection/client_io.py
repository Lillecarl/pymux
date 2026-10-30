"""
What a client's terminal is written through, and what it types read
from: the pipes between a client's application and its connection.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from typing import TYPE_CHECKING, Any, cast, override

from libpymux.protocol import Field, Message, Mode, Packet
from prompt_toolkit.input import PipeInput
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.output.vt100 import Vt100_Output
from pyte.cells import VS16, without_widening_selectors
from pyte.placeholders import PLACEHOLDER, blanked

from ..clusters import Drawn, fitted
from ..keys.keys import KittyVt100Parser

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPress

    from pymux.introspect import Counters

__all__ = ["ClientInput", "FrameOutput", "NoStdout", "SocketStdout"]

PIXEL_MOUSE_ON = "\x1b[?1016h"
PIXEL_MOUSE_OFF = "\x1b[?1016l"
FOCUS_REPORTS_ON = "\x1b[?1004h"
FOCUS_REPORTS_OFF = "\x1b[?1004l"
THEME_REPORTS_ON = "\x1b[?2031h"
THEME_REPORTS_OFF = "\x1b[?2031l"
INBAND_RESIZE_ON = "\x1b[?2048h"
INBAND_RESIZE_OFF = "\x1b[?2048l"


class SocketStdout:
    """
    Stdout-like object that writes everything through the unix socket to the
    client.
    """

    def __init__(
        self,
        send_packet: Callable[[Message], None],
        counters: Counters | None = None,
        frame_went_out: Callable[[], None] | None = None,
        after_frame: Callable[[int], str] | None = None,
    ) -> None:
        self.send_packet = send_packet
        self.counters = counters
        #: Told of each frame, for the count of the one client it goes to.
        self.frame_went_out = frame_went_out
        #: What goes behind each frame that holds anything.
        self.after_frame = after_frame
        self._buffer: list[str] = []

    def write(self, data: str) -> int:
        self._buffer.append(data)
        return len(data)

    def flush(self) -> None:
        # One flush is one frame: the renderer writes a whole frame and
        # then flushes it. So this is where a server can count what it
        # actually sent, which the invalidates cannot say -- several of
        # those become one frame.
        written = "".join(self._buffer)
        if written and self.after_frame is not None:
            written += self.after_frame(len(written))
        if self.counters is not None:
            self.counters.frame_went_out(len(written))
        if self.frame_went_out is not None:
            self.frame_went_out()

        self.send_packet({Field.CMD: Packet.OUT, Field.DATA: written})
        self._buffer = []

    def isatty(self) -> bool:
        return True


class FrameOutput(Vt100_Output):
    """
    A client's output that sends what a pane passed on after the frame.

    `forward_osc` queues a sequence here and `flush` puts it at the end
    of what goes out, behind the frame the renderer wrote. A sequence
    written when it arrived stood in front of that frame, so a
    clipboard changed before the screen showing the copy, and a harness
    that waits for a pane's notification as a fence still had to wait
    for silence after it. Lillecarl/pymux#579.

    **Only a frame's own flush carries it**: one made between the
    application's `before_render` and `after_render`, which
    `frame_begins` and `frame_ends` mark. Not every flush ends a frame:
    a bell, a reset of the renderer and graphics flush on their own, and
    a frame that the stale-frame check skips flushes nothing at all.
    Carried by any flush, the vterm middleman's fence went out ahead of
    the frame that drew the payload, and libvterm read an empty screen.
    Lillecarl/pymux#611.

    A pane draws an image cell as kitty's unicode placeholder, and
    `flush` turns each one into a space unless `keeps_placeholders`
    says this terminal reads them. Once per frame and not once per
    cell: the renderer writes cell by cell. Lillecarl/pymux#600.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._after_frame: list[str] = []
        self._in_frame = False
        self.keeps_placeholders: Callable[[], bool] = lambda: False
        #: Whether the terminal reports the mouse in pixels when the mouse
        #: is on. Lillecarl/pymux#648.
        self.pixel_mouse: Callable[[], bool] = lambda: False
        #: Whether U+FE0F goes after a one-column character, which this
        #: terminal would widen. Lillecarl/pymux#539.
        self.narrows_text_emoji: Callable[[], bool] = lambda: False
        #: How to fit each grapheme cluster to the width this terminal
        #: draws it: `TerminalReplies.cluster_fit`. Lillecarl/pymux#798.
        self.cluster_fit: Callable[[], str | Drawn | None] = lambda: None

    @override
    def enable_mouse_support(self) -> None:
        super().enable_mouse_support()
        if self.pixel_mouse():
            self.write_raw(PIXEL_MOUSE_ON)

    @override
    def disable_mouse_support(self) -> None:
        super().disable_mouse_support()
        # Always: a terminal left in pixel mode gives the next program
        # pixels where it expects cells.
        self.write_raw(PIXEL_MOUSE_OFF)

    # Focus, theme and size reports go with the alternate screen: on at
    # the first frame and at a redraw, off at a detach. A window that
    # gains the focus with a click iTerm2 keeps to itself is still a
    # client in use. Lillecarl/pymux#794, Lillecarl/pymux#796,
    # Lillecarl/pymux#797.
    @override
    def enter_alternate_screen(self) -> None:
        super().enter_alternate_screen()
        self.write_raw(FOCUS_REPORTS_ON + THEME_REPORTS_ON + INBAND_RESIZE_ON)

    @override
    def quit_alternate_screen(self) -> None:
        self.write_raw(FOCUS_REPORTS_OFF + THEME_REPORTS_OFF + INBAND_RESIZE_OFF)
        super().quit_alternate_screen()

    def write_after_frame(self, data: str) -> None:
        self._after_frame.append(data)

    def frame_begins(self) -> None:
        self._in_frame = True

    def frame_ends(self) -> None:
        self._in_frame = False

    @override
    def flush(self) -> None:
        if self._buffer:
            frame = "".join(self._buffer)
            if PLACEHOLDER in frame and not self.keeps_placeholders():
                frame = blanked(frame)
            if VS16 in frame and self.narrows_text_emoji():
                frame = without_widening_selectors(frame)
            fit = self.cluster_fit()
            if fit is not None:
                frame = fitted(frame, fit)
            self._buffer = [frame]
        if self._in_frame and self._after_frame:
            self.write_raw("".join(self._after_frame))
            self._after_frame = []
        super().flush()


class NoStdout:
    """
    Stdout-like object that keeps nothing: writes go nowhere.

    The temporary CLI of a socket command has no terminal behind it,
    and nothing it holds is ever drawn. But building the application
    builds a renderer, and prompt_toolkit's renderer answers its
    construction with a reset -- `show_cursor`, the cursor shape --
    written to the output and flushed there. On a connection that
    carries a command, the person reading the answer got those bytes
    after it, as garbage that came and went with the timing of the
    writes around it. Lillecarl/pymux#420.
    """

    def write(self, data: str) -> int:
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class ClientInput:
    """
    Input class that can be given to the CommandLineInterface.
    We only need this for turning the client into raw_mode/cooked_mode.
    """

    def __init__(
        self,
        send_packet: Callable[[Message], None],
        kitty_reply_callback: Callable[[str], None] | None = None,
        speaks_protocol: Callable[[], bool | None] | None = None,
        pixel_cell: Callable[[], tuple[int, int, int] | None] | None = None,
        pointer_moved: Callable[[], None] | None = None,
    ) -> None:
        self.send_packet = send_packet
        # Keep a reference to the context manager for the whole lifetime of
        # this object. `create_pipe_input()` returns a generator context
        # manager; when it's garbage collected, the pipe is closed.
        self._input_cm = create_pipe_input()
        self._input: PipeInput | None = self._input_cm.__enter__()

        # Replace the parser of the pipe input with one that also
        # understands the key encoding of the kitty keyboard protocol,
        # and routes terminal replies (keyboard flags query, device
        # attributes) to the given callback.
        vt100_input = cast(Vt100Input, self._input)
        #: When a person last pressed a key or used the mouse here: the
        #: parser hands terminal replies elsewhere, so they do not count.
        self.activity = time.time()

        def typed(key_press: KeyPress) -> None:
            self.activity = time.time()
            vt100_input._buffer.append(key_press)

        self._parser = KittyVt100Parser(
            typed,
            reply_callback=kitty_reply_callback,
            speaks_protocol=speaks_protocol,
            pixel_cell=pixel_cell,
            pointer_moved=pointer_moved,
        )
        vt100_input.vt100_parser = self._parser

    def send_text(self, data: str) -> None:
        "Feed a text string to the input pipe."
        if self._input is not None:
            self._input.send_text(data)

    @property
    def pointer_fraction(self) -> tuple[float, float]:
        "Where in its cell the last mouse report was."
        return self._parser.pointer_fraction

    @property
    def pointer_cell(self) -> tuple[int, int] | None:
        "The cell of the last mouse report."
        return self._parser.pointer_cell

    def close(self) -> None:
        "Close the input pipe. (Idempotent.)"
        if self._input is not None:
            with suppress(Exception):
                self._input_cm.__exit__(None, None, None)
            self._input = None

    @property
    def closed(self) -> bool:
        """
        Whether this input is closed. A closed pipe is closed.

        prompt_toolkit reads this before it takes keys, and it reads it
        after the pipe has gone: the key press that detached the client
        is still on the loop, and the application is still exiting.
        `__getattr__` forwarded that read to `None` and raised
        `AttributeError` on the terminal of the person who detached.
        `typeahead_hash` below carries the same guard for the same
        reason. Lillecarl/pymux#109.
        """
        if self._input is None:
            return True
        return self._input.closed

    def typeahead_hash(self) -> str:
        """
        The typeahead hash must keep working after the pipe is closed:
        prompt_toolkit stores unprocessed input as typeahead when the
        application exits, which can happen after the connection was
        closed.
        """
        if self._input is not None:
            return self._input.typeahead_hash()
        return "closed-pipe-input-%i" % id(self)

    # Implement raw/cooked mode by sending this to the attached client.

    def raw_mode(self) -> AbstractContextManager[None]:
        return self._create_context_manager(Mode.RAW)

    def cooked_mode(self) -> AbstractContextManager[None]:
        return self._create_context_manager(Mode.COOKED)

    def _create_context_manager(self, mode: Mode) -> AbstractContextManager[None]:
        "Create a context manager that sends 'mode' commands to the client."

        class mode_context_manager:
            def __enter__(*a: object) -> None:
                self.send_packet({Field.CMD: Packet.MODE, Field.DATA: mode})

            def __exit__(*a: object) -> None:
                self.send_packet({Field.CMD: Packet.MODE, Field.DATA: Mode.RESTORE})

        return mode_context_manager()

    def __getattr__(self, name: str) -> object:
        return getattr(self._input, name)
