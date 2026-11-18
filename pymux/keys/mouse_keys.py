"""
tmux's mouse keys: a mouse event named by what it was, its button and
where it happened, such as `MouseDown1Pane` or `WheelUpStatus`.

A binding on one runs in place of what pymux does with that event, and
`send-keys -M` in it hands the event back. The names, the case rule and
the modifiers are tmux's (key-string.c, `KEYC_MOUSE_STRING` in
tmux.h); `MouseClicks` counts clicks and drags the way
`server_client_check_mouse` in server-client.c does.
Lillecarl/pyterm#1034.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier

from .key_spelling import key_however_it_is_written

if TYPE_CHECKING:
    from pymux.core.pane import Pane
    from pymux.core.window_tree import Window

__all__ = [
    "CLICK_TIMEOUT",
    "MouseAt",
    "MouseClicks",
    "MouseKey",
    "MouseKind",
    "MouseLocation",
    "bound_key",
    "key_of",
    "mouse_at",
    "mouse_key",
    "the_mouse",
]


class MouseKind(StrEnum):
    "What the mouse did, as the name of the key starts."

    MOUSE_DOWN = "MouseDown"
    MOUSE_UP = "MouseUp"
    MOUSE_DRAG = "MouseDrag"
    MOUSE_DRAG_END = "MouseDragEnd"
    WHEEL_UP = "WheelUp"
    WHEEL_DOWN = "WheelDown"
    SECOND_CLICK = "SecondClick"
    DOUBLE_CLICK = "DoubleClick"
    TRIPLE_CLICK = "TripleClick"


class MouseLocation(StrEnum):
    "Where it happened, as the name of the key ends."

    PANE = "Pane"
    STATUS = "Status"
    STATUS_LEFT = "StatusLeft"
    STATUS_RIGHT = "StatusRight"
    STATUS_DEFAULT = "StatusDefault"
    SCROLLBAR_UP = "ScrollbarUp"
    SCROLLBAR_SLIDER = "ScrollbarSlider"
    SCROLLBAR_DOWN = "ScrollbarDown"
    EMPTY = "Empty"
    BORDER = "Border"
    CONTROL0 = "Control0"
    CONTROL1 = "Control1"
    CONTROL2 = "Control2"
    CONTROL3 = "Control3"
    CONTROL4 = "Control4"
    CONTROL5 = "Control5"
    CONTROL6 = "Control6"
    CONTROL7 = "Control7"
    CONTROL8 = "Control8"
    CONTROL9 = "Control9"


#: The buttons tmux names. 4 and 5 are the wheel, which has names of its own.
BUTTONS = (1, 2, 3, 6, 7, 8, 9, 10, 11)

_WHEELS = frozenset({MouseKind.WHEEL_UP, MouseKind.WHEEL_DOWN})

#: tmux's `KEYC_CLICK_TIMEOUT`, in seconds: how close two presses are to count as one click of several.
CLICK_TIMEOUT = 0.3


@dataclass(frozen=True, slots=True)
class MouseKey:
    "One mouse key. `str()` spells it as tmux prints it."

    kind: MouseKind
    button: int
    location: MouseLocation
    ctrl: bool = False
    meta: bool = False
    shift: bool = False

    def __str__(self) -> str:
        modifiers = "C-" * self.ctrl + "M-" * self.meta + "S-" * self.shift
        return "%s%s%s%s" % (modifiers, self.kind, self.button or "", self.location)


def _names() -> dict[str, tuple[MouseKind, int]]:
    "Every name before its location, lowered: tmux compares without case."
    names = {}
    for kind in MouseKind:
        for button in (0,) if kind in _WHEELS else BUTTONS:
            names[("%s%s" % (kind, button or "")).lower()] = (kind, button)
    return names


_BY_NAME = {
    (stem + location.lower()): (kind, button, location)
    for (stem, (kind, button)), location in itertools.product(_names().items(), MouseLocation)
}


def mouse_key(text: str) -> MouseKey | None:
    "The mouse key a name spells, or None. Modifiers as tmux reads them: `^`, then `C-`, `M-`, `S-` in any case."
    ctrl = meta = shift = False
    if text.startswith("^") and len(text) > 2:
        ctrl, text = True, text[1:]
    while len(text) > 2 and text[1] == "-":
        letter = text[0].lower()
        if letter == "c":
            ctrl = True
        elif letter == "m":
            meta = True
        elif letter == "s":
            shift = True
        else:
            return None
        text = text[2:]
    found = _BY_NAME.get(text.lower())
    if found is None:
        return None
    kind, button, location = found
    return MouseKey(kind, button, location, ctrl=ctrl, meta=meta, shift=shift)


def bound_key(text: str) -> tuple[str, ...] | MouseKey:
    """
    What `bind-key` binds for a name: a mouse key, or the
    prompt_toolkit keys of any other. Raises `ValueError` for neither.
    """
    found = mouse_key(text)
    if found is not None:
        return found
    return key_however_it_is_written(text)


_BUTTON_NUMBERS = {MouseButton.LEFT: 1, MouseButton.MIDDLE: 2, MouseButton.RIGHT: 3}


@dataclass
class MouseClicks:
    """
    One client's mouse, between its events: whether a button is held
    and dragging, and how many presses came close together.

    tmux's rules, from `server_client_check_mouse`:

    - A press is `MouseDown`; a second press of the same button in the
      same place inside `CLICK_TIMEOUT` is `SecondClick`, and a third
      is `TripleClick`.
    - `DoubleClick` comes after the second press, once `CLICK_TIMEOUT`
      passes with no third: `double_click_due` says when.
    - A move with a button held is `MouseDrag`, and the release that
      ends it is `MouseDragEnd` in place of `MouseUp`.
    """

    #: The button a drag holds, or 0 when none drags.
    dragging: int = 0
    #: Presses so far in the click being counted: 0, 1 or 2.
    presses: int = 0
    #: The button and the place the counted presses were at.
    press_at: tuple[int, MouseLocation, object] | None = None
    #: When the last counted press came, by `time.monotonic`.
    pressed_at: float = 0.0
    #: Counts the second presses, so a timer started for one can tell it is still the latest.
    second_presses: int = field(default=0)

    def kind_of(
        self, event: MouseEvent, location: MouseLocation, place: object, now: float | None = None
    ) -> tuple[MouseKind, int] | None:
        """
        The kind and button of one event, or None for one no key names:
        a move with no button held, or a button tmux has no name for.

        `place` says which pane or range it was over: a press in a
        different one starts a new click.
        """
        now = time.monotonic() if now is None else now
        button = _BUTTON_NUMBERS.get(event.button, 0)
        match event.event_type:
            case MouseEventType.SCROLL_UP:
                return MouseKind.WHEEL_UP, 0
            case MouseEventType.SCROLL_DOWN:
                return MouseKind.WHEEL_DOWN, 0
            case MouseEventType.MOUSE_MOVE:
                if button == 0:
                    return None
                self.dragging = button
                return MouseKind.MOUSE_DRAG, button
            case MouseEventType.MOUSE_UP:
                if self.dragging:
                    held, self.dragging = self.dragging, 0
                    return MouseKind.MOUSE_DRAG_END, held
                return (MouseKind.MOUSE_UP, button) if button else None
            case MouseEventType.MOUSE_DOWN:
                if button == 0:
                    return None
                self.dragging = 0
                at = (button, location, place)
                if self.presses and (self.press_at != at or now - self.pressed_at > CLICK_TIMEOUT):
                    self.presses = 0
                self.press_at, self.pressed_at = at, now
                if self.presses == 2:
                    self.presses = 0
                    return MouseKind.TRIPLE_CLICK, button
                self.presses += 1
                if self.presses == 2:
                    self.second_presses += 1
                    return MouseKind.SECOND_CLICK, button
                return MouseKind.MOUSE_DOWN, button
        return None

    def double_click_due(self, second_press: int) -> bool:
        "Whether the second press numbered `second_press` stands as a double click: no third press came."
        if self.second_presses != second_press or self.presses != 2:
            return False
        self.presses = 0
        return True


def key_of(event: MouseEvent, kind: MouseKind, button: int, location: MouseLocation) -> MouseKey:
    "The key one event makes, with its modifiers."
    return MouseKey(
        kind,
        button,
        location,
        ctrl=MouseModifier.CONTROL in event.modifiers,
        meta=MouseModifier.ALT in event.modifiers,
        shift=MouseModifier.SHIFT in event.modifiers,
    )


@dataclass(frozen=True)
class MouseAt:
    """
    The mouse event a binding runs for: what `-t =` names, what the
    `mouse_` formats read, and what `send-keys -M` hands back.

    `status_range` is tmux's `mouse_status_range`: `window`, `left`,
    `right`, or empty.
    """

    key: MouseKey
    pane: Pane | None = None
    window: Window | None = None
    status_range: str = ""
    #: What pymux does with the event when no binding takes it.
    replay: Callable[[], object] | None = None


_mouse_at: ContextVar[MouseAt | None] = ContextVar("mouse_at", default=None)


@contextmanager
def mouse_at(at: MouseAt) -> Iterator[None]:
    "Run the commands inside for this mouse event. A task started inside keeps it."
    token = _mouse_at.set(at)
    try:
        yield
    finally:
        _mouse_at.reset(token)


def the_mouse() -> MouseAt | None:
    "The mouse event the command running now is for, or None."
    return _mouse_at.get()
