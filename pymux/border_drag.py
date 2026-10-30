"""
Resize panes by dragging the line between them, as tmux does with
`mouse on`. Lillecarl/pymux#772.

**The drag owns the mouse from the press to the release.** One cell
off the line the pointer is over a pane, and a pane that has the mouse
hands the motion to its program. So the press writes a handler over
the whole screen at once, and while the drag lasts a float drawn over
everything writes it again on each frame.

**Each move sets the size from where the press found it**, not from
the last move: the weights are written as cells once, at the press,
and a move sets the two neighbours to those cells plus the distance
the pointer went. A delta per move would read a plan that may not yet
show the move before it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast, override

from prompt_toolkit.application.current import get_app
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from . import arrangement
from .actions import Kind, Origin, note, started_by

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    from .client_state import ClientState
    from .main import Pymux
    from .plane import Line, Seam

__all__ = ["BorderDrag", "DragCapture", "press_on_line"]


@dataclass
class BorderDrag:
    "One drag of one line, from the press."

    window: arrangement.Window
    seam: Seam
    #: Where the press was, across the line: a column or a row.
    start: int
    #: The cells of the two neighbours at the press. A strip column
    #: has a width instead.
    cells: tuple[int, int] = (0, 0)
    width: float = 0.0
    columns: int = 1

    @property
    def split(self) -> arrangement.HSplit | arrangement.VSplit:
        # A plan knows nothing of the arrangement; pymux's layouts put
        # a split there.
        return cast("arrangement.HSplit | arrangement.VSplit", self.seam.split)

    @property
    def in_strip(self) -> bool:
        return self.split is self.window.root and self.window.options.strip

    def move_to(self, at: int) -> None:
        moved = at - self.start
        split, index = self.split, self.seam.index
        if self.in_strip:
            # A column's width is its own: the columns past it move
            # along, and none of them shrinks.
            column = split[index]
            self.window.set_column_width(column, max(self.width + moved / self.columns, 2 / self.columns))
            return
        before, after = self.cells
        moved = max(1 - before, min(after - 1, moved))
        split.set_weight(split[index], before + moved)
        split.set_weight(split[index + 1], after - moved)


def press_on_line(pymux: Pymux, window: arrangement.Window, line: Line, event: MouseEvent) -> NotImplementedOrNone:
    "A press with the left button starts a drag of that line."
    seam = line.seam
    if seam is None or event.event_type is not MouseEventType.MOUSE_DOWN or event.button is not MouseButton.LEFT:
        return NotImplemented

    # Imported here: layout imports this module.
    from .geometry import room_for_panes, write_sizes_into_weights

    client_state = pymux.get_client_state()
    drag = BorderDrag(window, seam, event.position.x if seam.sideways else event.position.y)
    split = drag.split
    if drag.in_strip:
        drag.width = window.column_width(split[seam.index])
        drag.columns = room_for_panes(pymux, window).columns
    else:
        write_sizes_into_weights(pymux, window)
        drag.cells = (split.weight_of(split[seam.index]), split.weight_of(split[seam.index + 1]))
    client_state.border_drag = drag

    # The motion that comes before the next frame: that frame's float
    # writes the same handler.
    app = get_app()
    size = app.output.get_size()
    app.renderer.mouse_handlers.set_mouse_handler_for_range(
        0, size.columns, 0, size.rows, partial(_follow, pymux, client_state)
    )
    return None


def _follow(pymux: Pymux, client_state: ClientState, event: MouseEvent) -> NotImplementedOrNone:
    "Every mouse event while a drag is on: a move resizes, the release ends it."
    drag = client_state.border_drag
    if drag is None:
        return NotImplemented
    if event.event_type in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
        drag.move_to(event.position.x if drag.seam.sideways else event.position.y)
    if event.event_type is MouseEventType.MOUSE_UP:
        client_state.border_drag = None
        # The pointer kept the line's shape while the drag lasted.
        pymux.terminals.sync_pointer_shape()
        with started_by(Origin.MOUSE):
            note(pymux, Kind.CLICK, "drag-border")
    return None


def holds_window_panes(pymux: Pymux, window: arrangement.Window) -> bool:
    """
    Whether a drag of this window is on. A window pane hears its size
    at the release and not on each move: a size is a Wayland configure,
    and a program draws again for each.
    """
    return any(client.border_drag is not None and client.border_drag.window is window for client in pymux.clients)


class DragCapture(Container):
    "Over everything while this client drags a line, taking every mouse event."

    def __init__(self, pymux: Pymux, client_state: ClientState) -> None:
        self.pymux = pymux
        self.client_state = client_state

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
        if self.client_state.border_drag is None:
            return

        def capture() -> None:
            mouse_handlers.set_mouse_handler_for_range(
                write_position.xpos,
                write_position.xpos + write_position.width,
                write_position.ypos,
                write_position.ypos + write_position.height,
                partial(_follow, self.pymux, self.client_state),
            )

        # Deferred again, as `UrlLabels` is: the panes draw in a chain
        # of deferred draws, and the last handler set on a cell wins.
        screen.draw_with_z_index(z_index=z_index or 0, draw_func=capture)

    @override
    def get_children(self) -> list[Container]:
        return []
