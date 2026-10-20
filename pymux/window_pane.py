"""
A pane that shows a Wayland window instead of a terminal.

`Pane` is built around a terminal, and most of pymux reads it through
`pane.terminal`, `pane.process` and `pane.screen`: the title bar, the
status line, the formats. A window pane answers those with what a
window has -- its title, its app id -- and with nothing where a window
has nothing, so none of them needs to know. The few places that must
act differently ask `is_window_pane`. Lillecarl/pymux#620.

The pixels never come here. A kitty client gets them from
`ClientGraphics.render_windows`, as commands Rust built; this draws the
cells under the picture, and a notice where the client cannot show it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, override

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from pyte.keys import EventType, KeyEvent, Modifier, parse_key_data

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.key_binding import KeyPressEvent
    from prompt_toolkit.layout.containers import WritePosition
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen

    from pymux.wayland import WaylandServer

#: What a client that cannot draw kitty images sees in the pane.
NOTICE = "This pane shows a Wayland window; its terminal does not speak the kitty graphics protocol."

# Linux evdev key codes (`linux/input-event-codes.h`).
_SHIFT = 42
_CTRL = 29
_ALT = 56

#: A character on a US keyboard: its key, and whether shift makes it.
_CHARACTERS: dict[str, tuple[int, bool]] = {" ": (57, False)}
for _plain, _shifted, _first in (
    ("1234567890-=", "!@#$%^&*()_+", 2),
    ("qwertyuiop[]", "QWERTYUIOP{}", 16),
    ("asdfghjkl;'`", 'ASDFGHJKL:"~', 30),
    ("\\zxcvbnm,./", "|ZXCVBNM<>?", 43),
):
    for _offset, (_char, _shift_char) in enumerate(zip(_plain, _shifted, strict=True)):
        _CHARACTERS[_char] = (_first + _offset, False)
        _CHARACTERS[_shift_char] = (_first + _offset, True)

#: prompt_toolkit's named keys, as (modifiers, key).
_NAMED: dict[str, tuple[tuple[int, ...], int]] = {
    Keys.Enter: ((), 28),
    Keys.Tab: ((), 15),
    Keys.BackTab: ((_SHIFT,), 15),
    Keys.Escape: ((), 1),
    Keys.Backspace: ((), 14),
    Keys.Delete: ((), 111),
    Keys.Insert: ((), 110),
    Keys.Up: ((), 103),
    Keys.Down: ((), 108),
    Keys.Left: ((), 105),
    Keys.Right: ((), 106),
    Keys.Home: ((), 102),
    Keys.End: ((), 107),
    Keys.PageUp: ((), 104),
    Keys.PageDown: ((), 109),
    Keys.ShiftUp: ((_SHIFT,), 103),
    Keys.ShiftDown: ((_SHIFT,), 108),
    Keys.ShiftLeft: ((_SHIFT,), 105),
    Keys.ShiftRight: ((_SHIFT,), 106),
    Keys.ControlUp: ((_CTRL,), 103),
    Keys.ControlDown: ((_CTRL,), 108),
    Keys.ControlLeft: ((_CTRL,), 105),
    Keys.ControlRight: ((_CTRL,), 106),
    **{getattr(Keys, "F%i" % n): ((), code) for n, code in zip(range(1, 13), (*range(59, 69), 87, 88), strict=True)},
    # Not h, i or m: prompt_toolkit spells Backspace, Tab and Enter so.
    **{getattr(Keys, "Control" + c.upper()): ((_CTRL,), _CHARACTERS[c][0]) for c in "abcdefgjklnopqrstuvwxyz"},
}


def evdev_of(key: str) -> tuple[tuple[int, ...], int] | None:
    """
    The modifiers to hold and the key to press for one key press, or
    None for a key a keyboard cannot type.

    The keymap on the client's side is xkb's default, US. So a
    character is the key that types it there, with shift where shift
    makes it; anything else of the person's own layout is translated
    once already, by their terminal.
    """
    named = _NAMED.get(key)
    if named is not None:
        return named
    character = _CHARACTERS.get(key)
    if character is not None:
        code, shifted = character
        return ((_SHIFT,) if shifted else ()), code
    return None


def evdev_of_event(event: KeyEvent) -> tuple[tuple[int, ...], int] | None:
    """
    `evdev_of` for a key the terminal reported in the protocol's terms:
    the character key, with every modifier the event holds.

    pymux has no name for ctrl with a character that has no control
    code, so such a key comes as raw data. foot zooms on ctrl+=, ctrl+-
    and ctrl+0. Lillecarl/pymux#627.
    """
    if event.final != "u" or event.event == EventType.RELEASE or not 0 <= event.code < 0x110000:
        return None
    character = _CHARACTERS.get(chr(event.code))
    if character is None:
        return None
    code, shifted = character
    modifiers = [
        modifier
        for modifier, held in (
            (_SHIFT, shifted or event.mods & Modifier.SHIFT),
            (_CTRL, event.mods & Modifier.CTRL),
            (_ALT, event.mods & Modifier.ALT),
        )
        if held
    ]
    return tuple(modifiers), code


_BUTTONS = {MouseButton.LEFT: 0x110, MouseButton.RIGHT: 0x111, MouseButton.MIDDLE: 0x112}


def now() -> int:
    "A Wayland timestamp: milliseconds, from any fixed start."
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


class _Titles:
    "What `pane.screen.titles` gives the title bar: the window's title."

    def __init__(self) -> None:
        self.window = ""


class _Graphics:
    "A screen's images: a window pane has none of its own."

    images_by_id: dict = {}
    placements: dict = {}

    def virtual_placement(self, *_arguments):
        return None


class _PointerShapes:
    "The pointer the window asked for, by CSS name; with none, the terminal of the user picks."

    def __init__(self) -> None:
        self.shape = ""


class WindowScreen:
    "What a window pane answers where a terminal pane's screen would."

    def __init__(self) -> None:
        self.titles = _Titles()
        self.graphics = _Graphics()
        self.pointer_shapes = _PointerShapes()
        self.cursor_style_asked = False
        self.kitty_keyboard_flags = 0
        self.line_offset = 0

    def set_color_base(self, _base) -> None:
        "A window draws its own colours."


class _Backend:
    "No process id: the program runs as a client of the compositor."

    pid: int | None = None


class WindowProcess:
    "What a window pane answers where a terminal pane's process would."

    def __init__(self, widget: WindowWidget) -> None:
        self._widget = widget
        self.backend = _Backend()
        self.suspended = False
        self.is_terminated = False
        self.sx = 0
        self.sy = 0

    def kill(self) -> None:
        "Ask the window to close. The program decides; `closed` ends the pane."
        self._widget.close()

    def hang_up(self) -> None:
        "The same ask: a window gets no SIGHUP, and its close is already gentle."
        self.kill()

    def get_name(self) -> str:
        return self._widget.app_id

    def get_cwd(self) -> str | None:
        return None


class _NoControl:
    "Where a terminal pane keeps its control: `render-mode` writes here."

    keep_rows = True


class WindowControl(UIControl):
    "The cells under the window: blank for a kitty client, a notice otherwise."

    def __init__(self, widget: WindowWidget) -> None:
        self.widget = widget

    @override
    def is_focusable(self) -> bool:
        return True

    @override
    def create_content(self, width: int, height: int) -> UIContent:
        notice = not self.widget.wayland.client_shows_windows()
        words = NOTICE.split()
        lines: list[str] = []
        while notice and words:
            line = words.pop(0)
            while words and len(line) + 1 + len(words[0]) <= width:
                line += " " + words.pop(0)
            lines.append(line)

        def get_line(number: int) -> StyleAndTextTuples:
            return [("class:window-pane-notice", lines[number])] if number < len(lines) else []

        return UIContent(get_line=get_line, line_count=max(height, len(lines)), cursor_position=Point(0, 0))

    @override
    def get_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add(Keys.Any, filter=Condition(self.widget.takes_keys))
        def _(event: KeyPressEvent) -> None:
            for press in event.key_sequence:
                self.widget.press(press.key if press.key != Keys.Any else press.data)

        return bindings

    @override
    def mouse_handler(self, mouse_event: MouseEvent) -> None:
        # A click in a pane that had no focus selects it, as a terminal
        # pane's does (Lillecarl/pymux#527), and still goes to the window.
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            layout = get_app().layout
            if not layout.has_focus(self):
                layout.focus(self)
                if self.widget.on_mouse_focus is not None:
                    self.widget.on_mouse_focus()
        self.widget.mouse(mouse_event)


class _PaneWindow(Window):
    """
    The window pane's container: a click goes to the control at its cell
    in the pane, read from the screen and not from the content.

    prompt_toolkit's `Window` maps a click through the rows and columns
    its content drew, and this content draws nothing where the picture
    is: every click came to the first cell. Lillecarl/pymux#645.
    """

    @override
    def _write_to_screen_at_index(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
    ) -> None:
        super()._write_to_screen_at_index(screen, mouse_handlers, write_position, parent_style, erase_bg)
        control = self.content
        left, top = write_position.xpos, write_position.ypos

        def handler(mouse_event: MouseEvent) -> None:
            position = Point(x=mouse_event.position.x - left, y=mouse_event.position.y - top)
            control.mouse_handler(
                MouseEvent(
                    position=position,
                    event_type=mouse_event.event_type,
                    button=mouse_event.button,
                    modifiers=mouse_event.modifiers,
                )
            )

        mouse_handlers.set_mouse_handler_for_range(
            x_min=left,
            x_max=left + write_position.width,
            y_min=top,
            y_max=top + write_position.height,
            handler=handler,
        )


class WindowWidget:
    """
    What a window pane stores as `pane.terminal`: the container the
    layout draws, and the answers a terminal would give.
    """

    is_copying = False

    def __init__(self, wayland: WaylandServer, toplevel: int, takes_keys) -> None:
        self.wayland = wayland
        self.toplevel = toplevel
        self.takes_keys = takes_keys
        self.app_id = ""
        self.screen = WindowScreen()
        self.process = WindowProcess(self)
        self.terminal_control = _NoControl()
        self.control = WindowControl(self)
        # The default background, over `paint-screen`'s: kitty draws the
        # picture under every cell with a colour of its own (`WINDOW_Z`).
        self.window = _PaneWindow(self.control, always_hide_cursor=True, style="bg:default")
        #: The pane's size in cells, last told.
        self.columns = 0
        self.rows = 0
        #: Selects the pane when a click focuses it; the pane sets it.
        self.on_mouse_focus: Callable[[], None] | None = None

    def __pt_container__(self) -> Window:
        return self.window

    def set_size(self, width: int, height: int) -> None:
        "The pane's size in cells: the window is asked for that many pixels."
        if width <= 0 or height <= 0:
            return
        self.columns, self.rows = width, height
        self.process.sx, self.process.sy = width, height
        self.wayland.configure(self.toplevel, width, height)

    def enter_copy_mode(self) -> None:
        "A window has no history to copy from."

    def close(self) -> None:
        self.wayland.close(self.toplevel)

    def press(self, key: str) -> None:
        found = evdev_of(key)
        if found is None:
            # A character no key types on the client's keymap goes as
            # text; a named key with no evdev code goes nowhere.
            # Lillecarl/pymux#624.
            if not isinstance(key, Keys) and key.isprintable():
                self.wayland.type_text(self.toplevel, key)
            return
        modifiers, code = found
        self.wayland.press(self.toplevel, modifiers, code, now())

    def press_data(self, data: str) -> None:
        "The keys in a sequence the terminal sent, which pymux has no name for."
        for item in parse_key_data(data):
            found = evdev_of_event(item) if isinstance(item, KeyEvent) else None
            if found is not None:
                modifiers, code = found
                self.wayland.press(self.toplevel, modifiers, code, now())

    def mouse(self, event: MouseEvent) -> None:
        if event.event_type == MouseEventType.SCROLL_UP:
            self.wayland.scroll(self.toplevel, event.position, -1, now())
            return
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self.wayland.scroll(self.toplevel, event.position, 1, now())
            return
        pressed = event.event_type == MouseEventType.MOUSE_DOWN
        released = event.event_type == MouseEventType.MOUSE_UP
        button = _BUTTONS.get(event.button) if (pressed or released) else None
        self.wayland.pointer(self.toplevel, event.position, button, pressed, now())


def is_window_pane(pane) -> bool:
    return isinstance(pane.terminal, WindowWidget)
