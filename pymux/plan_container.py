"""
Draw a plan: every pane where the layout says it goes.

One container for two axes, doing what `ScrollableStrip` does for one.
A layout says where every pane is (`measure`) and where the view sits
on the plane (`look_at`); this puts each pane's container at its
rectangle, less the view's offset. Lillecarl/pymux#217.

**There is one coordinate space, the real one.** A pane that has
scrolled half off the left is written at a negative position, and the
renderer reads only the rectangle it is going to show, so the cells
outside it are written into a dictionary nobody reads.
`pymux/strip.py` says at length why the other way -- draw on a screen
of your own and copy the visible part over -- costs six kinds of bug,
one for each thing that hangs on a screen.

**A pane with no part of it in the view is not written at all.** The
clipping above is what makes a half-visible pane free; a pane that is
wholly outside is a different thing, because building its rows and
spelling its cells happens before any of them is thrown away.
Lillecarl/pymux#224.

**A pane that is half in the view is tinted**, so that a person can
see it is cut. That is the one case where what they see is not what
the program wrote, and the cells say nothing about it on their own.
The tint is not here: `View.cuts` is the question, and the answer goes
into the style of the pane itself (`layout.the_pane_is_cut`). Marking
the cells afterwards cannot work, because the panes are drawn in a
later pass of `Screen.draw_all_floats` than this method runs in.
Lillecarl/pymux#222.

**It holds the plan it drew.** A title bar is drawn *during* a frame,
inside one of these panes, so anything drawn in a frame can ask this
what the frame is: `select-pane -L` and a title bar then read the same
answer, which is the whole point of the work.
"""

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.key_binding import KeyBindingsBase
from prompt_toolkit.layout.containers import Container, to_container
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Char, Screen, WritePosition

from .plane import Pane, Plan, Rect, View

__all__ = ["PlanContainer"]


class PlanContainer(Container):
    """
    Show a view of a plan.

    :param layout: What says where the panes are. It answers
        `measure(available)` with a `Plan`, and
        `look_at(plan, view, focus)` with the plane coordinate that
        goes at the top left of the view.
    :param containers: One container for each pane, by pane. A slot
        holding a stack draws the pane it shows and no other, so the
        containers of the panes behind it are never asked to draw.
    :param tell_its_size: Called as `(pane, rect)` for every pane of
        the plan on every frame, drawn or not. **The plan is what sizes
        a pane**, and that has to be true of a pane it does not draw:
        the program in it needs the size it will be shown at, or it
        writes for a screen of the wrong shape until somebody looks at
        it. Decision 6 of `docs/layout-engine-plan.md`, and the fault
        the end to end checks caught when this container started
        skipping what nobody can see.
    :param view: Where this client looks at the plane. **It outlives
        this container**, which is why a caller owns it: the
        containers are built again whenever the arrangement changes
        shape, and a view of its own went back to the origin every
        time. Without one, this makes its own and a person scrolls a
        strip that forgets.
    :param room: How big the plane is, as a callable. **A plan is
        measured for the plane and drawn in the view**, and those are
        two sizes as soon as two clients of different sizes watch one
        window: a pane has one pty, so it has one size, and a client
        too small to see all of it scrolls. Without one the write
        position is the plane, which is what every client had while
        the plane was the smallest of them.
    """

    def __init__(
        self,
        layout,
        containers: dict,
        tell_its_size=None,
        view: View | None = None,
        room=None,
    ) -> None:
        self.layout = layout
        self.containers = {
            pane: to_container(container) for pane, container in containers.items()
        }
        self.tell_its_size = tell_its_size
        self.room = room

        #: Where this client looks at the plane, and how much of it it
        #: can see. A frame writes the size before it reads it, so a
        #: fresh view says nothing until one is drawn.
        self.view = View() if view is None else view

        #: The plan of the frame being drawn, for anything drawn inside
        #: it to read. `None` before the first frame.
        self.plan: Plan | None = None

        #: The size of the plane that plan was measured for. A title
        #: bar drawn in this frame reads the plan rather than working
        #: it out again, and this is what says the plan is still the
        #: answer: a client that resized between two frames has a plan
        #: of the size it was. Lillecarl/pymux#217.
        self.measured_for: Size | None = None

    def __repr__(self) -> str:
        return "PlanContainer(%r)" % (self.layout,)

    def reset(self) -> None:
        for container in self.containers.values():
            container.reset()

    def preferred_width(self, max_available_width: int) -> D:
        """
        Whatever it is given.

        The view scrolls, so it does not ask for the width the plan
        wants. Asking would make every parent try to supply it, which
        is the behaviour this exists to avoid.
        """
        return D(min=1)

    def preferred_height(self, width: int, max_available_height: int) -> D:
        "Whatever it is given, for the same reason."
        return D(min=1)

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        "Measure the plan, move the view onto the focus, and draw."
        seen = Size(rows=write_position.height, columns=write_position.width)
        plane = seen if self.room is None else self.room()

        self.plan = self.layout.measure(plane)
        self.measured_for = plane

        self.view.size = seen
        self.view.offset = self.layout.look_at(
            self.plan, self.view, self.focused_pane()
        )
        view = self.view.rect

        self._draw_the_chrome(screen, write_position, parent_style, view)

        for slot, rect in self.plan.rects.items():
            if self.tell_its_size is not None:
                # Every pane of the slot, shown or not, because a
                # hidden pane that already has the size it will be
                # shown at is revealed without a resize.
                for pane in slot.panes:
                    self.tell_its_size(pane, rect)

            if not rect.overlaps(view):
                # **A pane no part of which is in the view is not
                # drawn.** Writing it costs far more than the cells it
                # throws away: every row of it is built out of the
                # screen first, and every cell of those rows is
                # spelled. A strip of sixteen columns showed two of
                # them and paid three times what sixteen visible panes
                # of a divided window cost. Lillecarl/pymux#224.
                #
                # A pane that is *partly* in the view is drawn whole
                # and clipped, which is what the plane rests on.
                #
                # The cursor follows: nothing sets `show_cursor` unless
                # a window drew one, so a focused pane out of the view
                # leaves the cursor hidden. That is the decision
                # Lillecarl/pymux#201 made inside `ScrollablePane`.
                continue

            container = self.containers.get(slot.shown)
            if container is None:
                # A pane the caller gave no container for. Nothing can
                # be drawn for it, and a frame is not the place to find
                # that out.
                continue

            container.write_to_screen(
                screen,
                mouse_handlers,
                WritePosition(
                    xpos=write_position.xpos + rect.x - self.view.offset.x,
                    ypos=write_position.ypos + rect.y - self.view.offset.y,
                    width=rect.width,
                    height=rect.height,
                ),
                parent_style,
                erase_bg,
                z_index,
            )

    def _draw_the_chrome(
        self,
        screen: Screen,
        write_position: WritePosition,
        parent_style: str,
        view: Rect,
    ) -> None:
        """
        Fill the gaps the layout left, with what it says goes there.

        **A pane knows nothing about borders**, so this is where they
        are drawn, before the panes: a pane draws over its own
        rectangle, and a bar over the gap above or below it, and both
        win where they meet a line.

        A line that reaches into the view is written whole and the part
        outside it lands in a dictionary the renderer never reads. A
        line with no part of it in the view is not written at all: a
        strip draws one down the right of every column, and the columns
        it has scrolled past are most of them. Lillecarl/pymux#224.
        """
        chrome = getattr(self.layout, "chrome", None)
        if chrome is None or self.plan is None:
            return

        style = (parent_style + " class:border").strip()

        for line in chrome(self.plan):
            if not line.rect.overlaps(view):
                continue

            char = Char(line.char, style)
            top = write_position.ypos + line.rect.y - self.view.offset.y
            left = write_position.xpos + line.rect.x - self.view.offset.x

            for y in range(top, top + line.rect.height):
                row = screen.data_buffer[y]
                for x in range(left, left + line.rect.width):
                    row[x] = char

    def focused_pane(self) -> Pane | None:
        """
        Which pane has the keyboard, if one here does.

        `None` while a command line or a dialog holds it, and then the
        view stays where it is.
        """
        focused = get_app().layout.current_window

        for pane, container in self.containers.items():
            if _holds(container, focused):
                return pane
        return None

    def is_modal(self) -> bool:
        return False

    def get_key_bindings(self) -> KeyBindingsBase | None:
        return None

    def get_children(self) -> list[Container]:
        return list(self.containers.values())


def _holds(container: Container, window) -> bool:
    "Whether this container is that window, or holds it somewhere below."
    if container is window:
        return True
    return any(_holds(child, window) for child in container.get_children())
