"""
The small things the layout draws: the background, the big clock and
pane number inside a pane, the URL labels, and the message and question
lines at the bottom.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, override

from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText, StyleAndTextTuples
from prompt_toolkit.layout import screen as screen_module
from prompt_toolkit.layout.containers import (
    Container,
    Window,
    WritePosition,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from ptterm import Terminal

from pymux.core import window_tree

from ..format import format_pymux_string
from .z_index import Z_INDEX

if TYPE_CHECKING:
    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux

__all__ = [
    "MAX_MESSAGE_ROWS",
    "Background",
    "BigClock",
    "ConfirmationToolbar",
    "MessageToolbar",
    "PaneNumber",
    "UrlLabels",
]


class Background(Container):
    """
    Generate the background of dots, which becomes visible when several clients
    are attached and not all of them have the same size.

    (This is implemented as a Container, rather than a UIControl wrapped in a
    Window, because it can be done very effecient this way.)

    `paint_screen` colours the whole area with the theme's background as
    well; the dots keep their colour, because the pattern is what says a
    client here disagrees. Lillecarl/pymux#273.
    """

    def __init__(self, painted: Callable[[], bool] = lambda: False) -> None:
        self._painted = painted

    @override
    def reset(self) -> None:
        pass

    @override
    def preferred_width(self, max_available_width: int) -> D:
        return D()

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D()

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        "Fill the whole area of write_position with dots."
        whole = "class:background"
        if self._painted():
            whole += " class:painted"
        default_char = Char(" ", whole)
        dot = Char(".", whole)

        ypos = write_position.ypos
        xpos = write_position.xpos
        columns = range(xpos, xpos + write_position.width)

        # The pattern repeats every three rows. Build one row of each of
        # the three, and copy it. `dict.update` runs in C, which makes a
        # render of a wide screen about a tenth cheaper. The three are
        # rows of the kind a screen makes: a Rust row copies another Rust
        # row as pointers, and a dict about seven times slower. Not from
        # `data_buffer.default_factory`, which a clipped screen answers
        # with one shared row.
        data_buffer = screen.data_buffer
        rows = []
        for phase in range(3):
            row = screen_module._new_row(default_char)
            row.update({x: (dot if (x + phase) % 3 == 0 else default_char) for x in columns})
            rows.append(row)

        for y in range(ypos, ypos + write_position.height):
            data_buffer[y].update(rows[y % 3])

    @override
    def get_children(self) -> list[Container]:
        return []


# Numbers for the clock and pane numbering.
_numbers = list(
    zip(
        *[  # (Transpose x/y.)
            [
                "#####",
                "    #",
                "#####",
                "#####",
                "#   #",
                "#####",
                "#####",
                "#####",
                "#####",
                "#####",
            ],
            [
                "#   #",
                "    #",
                "    #",
                "    #",
                "#   #",
                "#    ",
                "#    ",
                "    #",
                "#   #",
                "#   #",
            ],
            [
                "#   #",
                "    #",
                "#####",
                "#####",
                "#####",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
            ],
            [
                "#   #",
                "    #",
                "#    ",
                "    #",
                "    #",
                "    #",
                "#   #",
                "    #",
                "#   #",
                "    #",
            ],
            [
                "#####",
                "    #",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
                "    #",
                "#####",
                "#####",
            ],
        ]
    )
)


def _draw_number(
    screen: Screen, x_offset: int, y_offset: int, number: int, style: str = "class:clock", transparent: bool = False
) -> None:
    "Write number at position."
    fg = Char(" ", "class:clock")
    bg = Char(" ", "")

    for y, row in enumerate(_numbers[number]):
        screen_row = screen.data_buffer[y + y_offset]
        for x, n in enumerate(row):
            if n == "#":
                screen_row[x + x_offset] = fg
            elif not transparent:
                screen_row[x + x_offset] = bg


class BigClock(Container):
    """
    Display a big clock.
    """

    WIDTH = 28
    HEIGHT = 5

    def __init__(self, pymux: Pymux, on_click: Callable[[], None]):
        self.pymux = pymux
        self.on_click = on_click

    @override
    def reset(self):
        pass

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        xpos = write_position.xpos
        ypos = write_position.ypos

        # Erase background.
        bg = Char(" ", "")

        def draw_func() -> None:
            for y in range(ypos, self.HEIGHT + ypos):
                row = screen.data_buffer[y]
                for x in range(xpos, xpos + self.WIDTH):
                    row[x] = bg

            # Display time. Test-mode pins the clock, so a picture
            # of a pane in clock-mode cannot race it.
            now = self.pymux.displayed_now()
            _draw_number(screen, xpos + 0, ypos, now.hour // 10)
            _draw_number(screen, xpos + 6, ypos, now.hour % 10)
            _draw_number(screen, xpos + 16, ypos, now.minute // 10)
            _draw_number(screen, xpos + 23, ypos, now.minute % 10)

            # Add a colon
            screen.data_buffer[ypos + 1][xpos + 13] = Char(" ", "class:clock")
            screen.data_buffer[ypos + 3][xpos + 13] = Char(" ", "class:clock")

            screen.width = self.WIDTH
            screen.height = self.HEIGHT

            mouse_handlers.set_mouse_handler_for_range(
                x_min=xpos,
                x_max=xpos + write_position.width,
                y_min=ypos,
                y_max=ypos + write_position.height,
                handler=self._mouse_handler,
            )

        screen.draw_with_z_index(z_index=Z_INDEX.CLOCK, draw_func=draw_func)

    def _mouse_handler(self, mouse_event: MouseEvent) -> None:
        "Click callback."
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.on_click()

    @override
    def preferred_width(self, max_available_width: int) -> D:
        return D.exact(BigClock.WIDTH)

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D.exact(BigClock.HEIGHT)

    @override
    def get_children(self) -> list[Container]:
        return []


class PaneNumber(Container):  # XXX: make FormattedTextControl
    """
    Number of panes, to be drawn in the middle of the pane.
    """

    WIDTH = 5
    HEIGHT = 5

    def __init__(self, pymux: Pymux, arrangement_pane: window_tree.Pane) -> None:
        self.pymux = pymux
        self.arrangement_pane = arrangement_pane

    @override
    def reset(self) -> None:
        pass

    def _get_index(self):
        window = self.pymux.arrangement.get_active_window()
        if window is None:
            return 0

        try:
            return window.get_pane_index(self.arrangement_pane)
        except ValueError:
            return 0

    @override
    def preferred_width(self, max_available_width: int) -> D:
        # Enough to display all the digits.
        return D.exact(6 * len("%s" % self._get_index()) - 1)

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D.exact(self.HEIGHT)

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        style = "class:panenumber"

        def draw_func():
            for i, d in enumerate("%s" % (self._get_index(),)):
                _draw_number(
                    screen,
                    write_position.xpos + i * 6,
                    write_position.ypos,
                    int(d),
                    style=style,
                    transparent=True,
                )

        screen.draw_with_z_index(z_index=Z_INDEX.PANE_NUMBER, draw_func=draw_func)

    @override
    def get_children(self) -> list[Container]:
        return []


class UrlLabels(Container):
    """
    The labels of URL mode, over the first cells of each link. They are
    placed after the panes drew, from where each pane says it drew the
    link this frame, so a label follows its link as the pane scrolls.
    Lillecarl/pymux#608.
    """

    def __init__(self, client_state: ClientState) -> None:
        self.client_state = client_state

    @override
    def reset(self) -> None:
        pass

    @override
    def preferred_width(self, max_available_width: int) -> D:
        return D.exact(1)

    @override
    def preferred_height(self, width: int, max_available_height: int) -> D:
        return D.exact(1)

    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        mode = self.client_state.url_mode
        if mode is None:
            return

        terminal = mode.pane.terminal
        if not isinstance(terminal, Terminal):
            return

        def draw_func() -> None:
            for target in mode.waiting():
                place = terminal.where_shown(target.link.row, target.link.column)
                if place is None:
                    continue
                y, x = place
                # Past the edge is drawn too: the renderer leaves out the
                # columns the terminal does not have, and the screen's
                # width is not known while the floats draw.
                for offset, letter in enumerate(target.label[len(mode.typed) :]):
                    screen.data_buffer[y][x + offset] = Char(letter, "class:url-label")

        screen.draw_with_z_index(z_index=Z_INDEX.URL_LABEL, draw_func=draw_func)

    @override
    def get_children(self) -> list[Container]:
        return []


#: How many rows a message may take. It is a pop-up over the panes, so
#: it may not become a page: past this the end of a very long message
#: is cut, which is the end a reader reaches last.
MAX_MESSAGE_ROWS = 5


class MessageToolbar(Window):
    """
    Pop-up (at the bottom) for showing error/status messages.

    **It wraps, and it does not park a cursor at the end.** This was a
    `FormattedTextToolbar`, which is a `Window` that does not wrap, and
    the message carried a `[SetCursorPosition]` marker after the text. A
    window scrolls to keep its cursor visible whether it has the focus
    or not (`Window._scroll_without_linewrapping`), so a message wider
    than the terminal was shown from its *end*.

    That lost the front of it. `Pymux.report_startup_errors` joins every
    failed line of a configuration file into one message, and each one
    names the file and the line, so two of them pass the width of a
    normal terminal easily -- and only the last could be read. A person
    fixed that one, ran pymux again, and met the first.
    Lillecarl/pymux#205, and the last step of Lillecarl/pymux#38.

    Nothing is typed into a message, so the cursor had no work to do
    there. The y/n toolbar keeps its marker: that one is a prompt, and
    it is short.
    """

    def __init__(self, client_state: ClientState) -> None:
        def get_message() -> str:
            # If there is a message to be shown for this client, show that.
            if client_state.message:
                return client_state.message
            return ""

        def get_tokens() -> AnyFormattedText:
            message = get_message()
            if message:
                return FormattedText([("class:message", message + " ")])
            return ""

        # The style rides on the fragments, the way it did before: the
        # toolbar itself carried none, so the rows past the end of the
        # message are the background and not a coloured bar.
        super().__init__(
            FormattedTextControl(get_tokens),
            wrap_lines=True,
            dont_extend_height=True,
            height=D(min=1, max=MAX_MESSAGE_ROWS),
        )


class ConfirmationToolbar(FormattedTextControl):
    """
    Window that displays the yes/no confirmation dialog.
    """

    def __init__(self, pymux: Pymux, client_state: ClientState) -> None:
        def get_tokens() -> StyleAndTextTuples:
            return [
                ("class:question", " "),
                (
                    "class:question",
                    format_pymux_string(pymux, client_state.confirm_text or "", client=client_state),
                ),
                ("class:question", " "),
                ("class:yesno", "  y/n"),
                ("[SetCursorPosition]", ""),
                ("class:yesno", "  "),
            ]

        super().__init__(get_tokens, style="class:confirmationtoolbar")
