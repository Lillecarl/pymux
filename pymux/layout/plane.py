"""
The plane: one place that answers "where is every pane".

`docs/layout-engine-plan.md` holds the design and Lillecarl/pymux#217
holds the argument for it. The short form: four places decide geometry
today, and the clearest symptom is that "which pane is beside this
one" has two answers, one read off the last frame and one walked out
of the tree. A plane answers it once.

**The plane is unbounded.** `x` and `y` may be negative, and a
rectangle needs no permission from a screen size. `View` is what is
bounded: one client's terminal, over part of the plane.

This module is the bottom of that work, and it takes two NamedTuples
from prompt_toolkit and nothing else: no widget, no pty, no screen. It
holds five things and almost no behaviour, and `rects.py` the
arithmetic under them.

    Rect     a rectangle of cells on the plane, in `rects.py`
    Slot     one rectangle's worth of it, and the panes in it
    Layer    one plane: every slot on it, and the services
    Plan     the stack of planes, for one frame
    View     where one client looks, and how much it can see

**A pane is any object with an identity.** Nothing here asks a pane
for anything, so nothing here imports one. `pymux.arrangement` passes
its own `Pane` and a test passes a stand-in.

**A slot is the thing that has a rectangle, not a pane.** More than
one pane in a slot means tabbed: one shown, the rest behind it. Carl:
"tabstacking on rectangles is handled by an invisible wrapper element
that owns 1 or more terminals". So "no two slots overlap" needs no
word about which pane is visible, and moving a stack is one operation
on one object.

**Nothing here can stop a popup.** A popup draws over the panes and
always has, in prompt_toolkit's own stack: the chrome floats sit at
`Z_INDEX` 5 to 9, and `display-popup -E` opens `Session.overlay_pane`,
which is not in the window tree at all. That stack is not this one --
`Layer` numbers what the window owns.

**"No two slots overlap" is a rule about one plane.** A plan is a
stack of planes, numbered, and a higher number draws over a lower one.
Carl: "we have multiple separate planes, the higher up the Z plane we
are the higher our drawing priority is", and "rectangles can't overlap
on the same plane". That second half is what keeps this module's
services meaning anything: "the one to my left" has an answer inside a
plane and none between two, so `neighbour`, `trace` and the numbering
all work on one plane at a time.

A floating *window* -- a pane a person parks somewhere and leaves
there -- is therefore not a second list beside `rects`. It is a slot
on a higher plane. Lillecarl/pymux#228.

**What is not here.** No layout: a plan arrives already laid out, and
the classes that lay one out (`Strip`, `Divided`, `Masonry`) come in
the slices after this one. So nothing in this module validates
geometry. Two slots that overlap **on one layer** are a fault of
whatever laid it out, and `tests/test_plane.py` is where that promise
is kept.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import NamedTuple, cast

from prompt_toolkit.data_structures import Point, Size

from .rects import Rect, Side, bounding_box, covered, enters, inside, overlap_of, towards

__all__ = [
    "COLUMN_RESIZE",
    "GROUND",
    "RESIZE_SHAPES",
    "ROW_RESIZE",
    "Layer",
    "Line",
    "Pane",
    "Plan",
    "Seam",
    "Slot",
    "View",
]


#: What a slot holds, as an annotation: any object with an identity,
#: because nothing here asks a pane for anything.
#:
#: **This is not `core.pane.Pane`**, and it is not exported, so that
#: nobody imports the word from here and gets `object`. It says what
#: the annotations below mean and no more.
Pane = object


class Line(NamedTuple):
    """
    A run of one character, in a gap a layout left.

    **A pane knows nothing about borders.** Carl: "individual panes
    should not be aware of borders ... the layout is responsible for
    drawing the borders either way." So a layout says where its lines
    go and what fills them, and the container that draws a plan paints
    them.

    A slot may draw a header and a footer of its own -- the bar over a
    pane and the bar under it -- and where those are drawn a line
    between two panes is not needed, because the bars already separate
    them.
    """

    rect: Rect
    char: str
    #: What a drag on the line moves. None for a line nothing moves.
    seam: Seam | None = None


#: The pointer shapes of a line a drag moves, as OSC 22 names them. The
#: X11 names, which kitty (`kitty/screen.c`, the css to enum list) and
#: iTerm2 (`PTYSession.m`, `cursors`) both read. CSS's col-resize is in
#: neither, and iTerm2 knows no CSS name at all; each drops a name it lacks.
COLUMN_RESIZE = "sb_h_double_arrow"


ROW_RESIZE = "sb_v_double_arrow"


RESIZE_SHAPES = frozenset((COLUMN_RESIZE, ROW_RESIZE))


class Seam:
    """
    What a line divides: the child of `split` at `index` and the one
    after it. A strip's line divides a column from the room past it.

    **Equal by the split's identity**, not its contents: a plan is
    compared with the last one on every frame, and a split is a list
    of everything under it. Lillecarl/pymux#772.
    """

    __slots__ = ("index", "sideways", "split")

    def __init__(self, split: object, index: int, sideways: bool) -> None:
        self.split = split
        self.index = index
        #: The line runs down, so a drag moves columns.
        self.sideways = sideways

    @property
    def pointer_shape(self) -> str:
        "The OSC 22 shape of a pointer over the line. Lillecarl/pymux#819."
        return COLUMN_RESIZE if self.sideways else ROW_RESIZE

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Seam) and self.split is other.split and self.index == other.index

    def __hash__(self) -> int:
        return hash((id(self.split), self.index))

    def __repr__(self) -> str:
        return "Seam(%s, %d)" % (type(self.split).__name__, self.index)


class View:
    """
    The part of the plane one client can see.

    **A plan is shared, and a view is not.** A pane has one pty, so it
    has one size however many clients look at it -- decision 10 of
    `docs/layout-engine-plan.md`. Two clients of different sizes
    therefore see two different parts of the same plan, and nothing a
    view does changes a plan.

    **A view outlives a frame.** The container that draws moves it onto
    the pane a person is typing in, and `LayoutManager` keeps one per
    window, so a strip stays where a person scrolled it when a pane
    opens or closes and the containers are built again.

    **No marks yet.** Decision 12 gives a view a dictionary of named
    offsets to jump back to. Nothing asks for one, so it is not here.
    """

    def __init__(
        self,
        offset: Point = Point(x=0, y=0),
        size: Size = Size(rows=0, columns=0),
    ) -> None:
        #: The plane coordinate the top left of this view shows.
        self.offset = offset

        #: How many cells of the plane this view can show. It is the
        #: client's terminal, less what the chrome around the panes
        #: takes, and a frame writes it before it reads it.
        self.size = size

        #: The offset a frame drew at, which trails `offset` while the
        #: view glides to it. `offset` is where the view is going, so
        #: `moved_onto` judges a pane against where the view will be
        #: and not against a point it is passing. `None` before the
        #: first frame.
        self.shown: Point | None = None

        #: The glide under way: where it left from, where it goes, and
        #: when it started, in the drawing container's clock.
        self.glide: tuple[Point, Point, float] | None = None

    @property
    def shown_rect(self) -> Rect:
        "What this frame shows of the plane, which a glide moves."
        shown = self.offset if self.shown is None else self.shown
        return Rect(x=shown.x, y=shown.y, width=self.size.columns, height=self.size.rows)

    def __repr__(self) -> str:
        return "View(%r, %r)" % (self.offset, self.size)

    @property
    def rect(self) -> Rect:
        "This view as a rectangle of the plane."
        return Rect(
            x=self.offset.x,
            y=self.offset.y,
            width=self.size.columns,
            height=self.size.rows,
        )

    def shows(self, rect: Rect) -> bool:
        """
        Whether any cell of that rectangle is in this view.

        This is what says a pane is worth drawing, and later what says
        a client is worth waking. Lillecarl/pymux#224.
        """
        return self.rect.overlaps(rect)

    def cuts(self, rect: Rect) -> bool:
        """
        Whether this view shows part of that rectangle and not all of
        it.

        **A cut pane is the one case where what a person sees is not
        what the program wrote**, and the cells give nothing away: a
        pane whose program wrote nothing past the cut looks exactly
        like a pane that ends there. Lillecarl/pymux#222.
        """
        return self.shows(rect) and not self.rect.encloses(rect)

    def moved_onto(self, rect: Rect | None, plane: Rect) -> Point:
        """
        Where this view goes to show that rectangle, on that plane.

        `rect` is the thing a person is looking at, and `None` means
        there is nothing to follow: a command line or a dialog holds
        the keyboard, and the view then only stays on the plane.

        **A rectangle already wholly inside the view moves nothing.**
        That is what makes walking to a pane and back a round trip,
        rather than dragging the view a little every time the focus
        lands somewhere it can already see. Lillecarl/pymux#207.

        **A rectangle too big to show whole shows its start.** Carl:
        "left should generally be preferred for terminals since that's
        where ~100% of applications begin writing text, it's even
        likely that a missing right column doesn't miss anything."
        Lillecarl/pymux#218. The same rule holds down the page, for
        the same reason.

        Every layout shares this. A tiling measured to fit its view
        gets the origin out of it, because the plane is then the view.
        """
        x, y = self.offset.x, self.offset.y

        if rect is not None:
            x = towards(x, self.size.columns, rect.x, rect.right)
            y = towards(y, self.size.rows, rect.y, rect.bottom)

        return Point(
            x=inside(x, self.size.columns, plane.x, plane.right),
            y=inside(y, self.size.rows, plane.y, plane.bottom),
        )


def _place_of(panes: list[Pane], pane: Pane) -> int:
    """
    Where this pane sits in the list, by identity.

    `list.index` asks whether two panes are equal, and a stand-in for
    a pane in a test may well answer yes to another one. Identity is
    what a pane has and what this needs. `window_tree._place_of` is
    the same function, for the same reason.
    """
    for place, held in enumerate(panes):
        if held is pane:
            return place
    raise ValueError("%r is not in %r" % (pane, panes))


class Slot:
    """
    One rectangle's worth of the plane, and the panes in it.

    A slot owns one or more panes and shows exactly one of them. More
    than one means tabbed, and nothing else: a slot never holds a
    layout of its own, so there is no recursion here.

    **Every pane of a slot has the slot's rectangle**, shown or not. A
    pty needs a size, and giving a hidden pane the size it will have
    means revealing it costs no `SIGWINCH` and no redraw.

    A slot is identified by being itself. Two slots holding the same
    panes are two slots, so `Plan.rects` can key on one.
    """

    def __init__(self, *panes: Pane) -> None:
        if not panes:
            raise ValueError("a slot owns at least one pane")

        self.panes = list(panes)

        #: Which pane a person sees, as a place in `panes`.
        self.showing = 0

    @property
    def shown(self) -> Pane:
        "The one pane a person sees."
        return self.panes[self.showing]

    @property
    def stacked(self) -> bool:
        "True when this slot holds more than one pane."
        return len(self.panes) > 1

    def add(self, pane: Pane, after: Pane | None = None) -> None:
        """
        Put another pane in this slot, so the slot is tabbed.

        `after` names the pane it goes behind, and the default is the
        end of the stack. What a person sees does not change: opening
        a tab and showing it are two things, and `show` is the other
        one.
        """
        if after is None:
            self.panes.append(pane)
            return

        self.panes.insert(_place_of(self.panes, after) + 1, pane)
        if _place_of(self.panes, pane) <= self.showing:
            self.showing += 1

    def remove(self, pane: Pane) -> None:
        """
        Take a pane out of this slot.

        **A slot refuses to empty itself.** A slot with no pane has a
        rectangle and nothing to draw in it, which no invariant
        allows, so the last pane leaving means the *slot* goes. That
        is the caller's move, and it is the one place that knows what
        holds the slot.

        Taking the shown pane out shows the one behind it, the way a
        tab bar does when a person closes the tab they are on.
        """
        if len(self.panes) == 1:
            raise ValueError("the last pane of a slot leaves with the slot")

        place = _place_of(self.panes, pane)
        del self.panes[place]

        if place < self.showing or self.showing >= len(self.panes):
            self.showing -= 1

    def show(self, pane: Pane) -> None:
        "Bring one of this slot's panes to the front."
        self.showing = _place_of(self.panes, pane)

    def __contains__(self, pane: Pane) -> bool:
        return any(held is pane for held in self.panes)

    def __len__(self) -> int:
        return len(self.panes)

    def __repr__(self) -> str:
        if not self.stacked:
            return "Slot(%r)" % (self.shown,)
        return "Slot(%r, showing %r)" % (self.panes, self.shown)


#: The plane a window's own panes are laid out on. Every layout
#: writes here, and it is the only plane anything builds today.
GROUND = 0


#: What `Plan.neighbour` has not been asked yet. `None` is an answer.
_UNASKED = object()


class Layer:
    """
    One plane: where every slot on it is, and what that lets a person
    ask.

    **No two slots of a layer overlap.** Carl: "rectangles can't
    overlap on the same plane". That is what makes the questions below
    mean anything: "the one to my left" has an answer among
    rectangles that do not overlap, and none among rectangles that do.
    Two *planes* may cover each other as much as they like, which is
    what `Plan` is the stack of.

    Nothing here checks it. A layer holding two overlapping slots is a
    fault of whatever laid it out, and `tests/test_plane.py` is where
    that promise is kept.
    """

    def __init__(self, rects: dict[Slot, Rect] | Iterable[tuple[Slot, Rect]]) -> None:
        self.rects: dict[Slot, Rect] = dict(rects)

        #: The bounding box of every rectangle. A plane is unbounded,
        #: so this is where the *panes* are and not where the plane
        #: ends.
        self.bounds: Rect = bounding_box(self.rects.values())

    @property
    def slots(self) -> list[Slot]:
        "Every slot, in the order it was put on the plane."
        return list(self.rects)

    @property
    def panes(self) -> list[Pane]:
        "Every pane on this plane, the hidden ones as well."
        return [pane for slot in self.rects for pane in slot.panes]

    @property
    def shown(self) -> list[Pane]:
        "The one pane of each slot a person can see."
        return [slot.shown for slot in self.rects]

    def at(self, point: Point) -> Slot | None:
        """
        The slot that holds that cell, if a slot does.

        A hole is allowed on the plane, so `None` is a real answer.
        """
        for slot, rect in self.rects.items():
            if rect.holds(point):
                return slot
        return None

    def neighbour(self, slot: Slot, side: Side) -> Slot | None:
        """
        The slot a person means by "the one to my left", and so on.

        **Strict.** Only a slot that is beyond this edge and whose
        band overlaps this one can answer. Of those, the smallest gap
        wins; a tie goes to the largest overlap, and then to whichever
        went on the plane first. If nothing overlaps this band the
        answer is nothing: no diagonal jump. tmux is strict here, and
        it is the less surprising of the two.

        A gap of zero is the ordinary case, because two rectangles of
        a tiling touch.
        """
        mine = self.rects[slot]
        best: tuple[tuple[int, int], Slot] | None = None

        for other, rect in self.rects.items():
            if other is slot:
                continue

            gap = mine.gap_to(rect, side)
            if gap < 0:
                continue

            overlap = overlap_of(mine.span(side), rect.span(side))
            if overlap == 0:
                continue

            key = (gap, -overlap)
            if best is None or key < best[0]:
                best = (key, other)

        return None if best is None else best[1]

    def trace(self, origin: Point, angle: float) -> Slot | None:
        """
        The first slot a ray from that cell runs into.

        This is the general answer, and `neighbour` is the cheap exact
        one for the four directions a key is bound to. Carl asked for
        both: "tracing diagonally (even on floating point angles)
        shouldn't be blocked, just not the default (integral tracing
        is cheaper)."

        The ray leaves the middle of `origin`, so it never runs along
        an edge. `angle` is in radians, zero points right, and it
        grows clockwise on the screen because `y` grows downwards. A
        slot that holds `origin` is not an answer; a tie goes to
        whichever slot went on the plane first.
        """
        start = (origin.x + 0.5, origin.y + 0.5)
        step = (math.cos(angle), math.sin(angle))
        best: tuple[float, Slot] | None = None

        for slot, rect in self.rects.items():
            if rect.holds(origin):
                continue

            when = enters(rect, start, step)
            if when is None:
                continue

            if best is None or when < best[0]:
                best = (when, slot)

        return None if best is None else best[1]

    def __repr__(self) -> str:
        return "Layer(%r)" % (self.rects,)


class Plan:
    """
    Where every slot is, and what that lets a person ask.

    A plan is a value: whatever laid it out is finished with it, and
    reading it changes nothing. A layout hands one over on every
    frame, and everything that draws, moves the focus or names a
    neighbour reads this and nothing else.

    **A plan is a stack of planes.** Carl: "we have multiple separate
    planes, the higher up the Z plane we are the higher our drawing
    priority is". The key is the plane's number, a higher one draws
    over a lower one, and `GROUND` is where every layout writes today.
    A floating window is therefore a slot on a higher plane and not a
    second list beside the panes. Lillecarl/pymux#228.

    **These numbers are not prompt_toolkit's `Z_INDEX`.** A plan
    stacks what the *window* owns. The chrome around it -- the status
    bar, the message toolbar, a popup -- floats over the whole layout
    and keeps the toolkit's own numbering, in `z_index.Z_INDEX`.

    **The numbers are derived, not held, and focus never changes one.**
    Carl decided this before anything builds a float
    (Lillecarl/pymux#357). A window keeps its floats in an order, and
    `measure()` numbers the planes from that order on every frame:
    `GROUND` for the tiled tree, then 1, 2, 3 for the floats, bottom to
    top. A float is appended when it opens, moved to the end when it is
    raised, and dropped when it closes. **Focus does none of those.**

    Two things follow, and both are why a numbered dictionary is enough
    here where tmux needs a list it can reorder:

    - The numbers never grow. Three floats always give planes 0 to 3,
      however often anything has been raised, because nothing carries a
      number from one frame to the next.
    - No empty plane is ever built, so nothing has to remove one.

    tmux raises a floating pane on focus and never touches a tiled one
    (`window_redraw_active_switch`, `window.c:790`). We keep the half
    that matches and leave the other out: `select-pane` moves the
    focus and nothing else.

    **A pane may ask to be moved; it may not move itself.** Carl:
    elements "communicate with the layout and **ask** to be moved
    (which can be denied if we ever have a reason to do so)". Nothing
    here writes to a plan, so the ask goes to whatever laid the plan
    out and that decides. `Pymux.resize_pane_to_program_request` is the
    same shape already: a program asks for a size, and it only gets one
    when `allow-program-resize` says so, because a pane sits in a
    layout and taking room takes it from somebody.

    """

    def __init__(
        self,
        layers: dict[int, dict[Slot, Rect] | Iterable[tuple[Slot, Rect]]],
        lines: Iterable[Line] = (),
    ) -> None:
        #: One `Layer` for each plane, lowest number first, which is
        #: the order they paint in.
        self.layers: dict[int, Layer] = {number: Layer(rects) for number, rects in sorted(layers.items())}

        #: The borders in the gaps the layout left. The walk that
        #: leaves a gap emits its line, so the two cannot drift apart.
        self.lines: tuple[Line, ...] = tuple(lines)

        planes = list(self.layers.values())

        #: Every slot of every plane, back to front -- a slot later in
        #: here draws over an earlier one -- and the box that holds
        #: them all. A plane is unbounded, so the box is where the
        #: *panes* are and not where the plane ends.
        if len(planes) == 1:
            # One plane is every plan anything builds today, and a
            # plan is measured on every frame (Lillecarl/pymux#336).
            # So the flat view *is* that plane's dictionary and its box
            # is the plan's box. Neither is ever written to.
            self.rects: dict[Slot, Rect] = planes[0].rects
            self.bounds: Rect = planes[0].bounds
        else:
            self.rects = {slot: rect for layer in planes for slot, rect in layer.rects.items()}
            self.bounds = bounding_box(self.rects.values())

        #: Which slot each pane is in, and which plane each slot is
        #: on. Built once, because everything that asks where a pane
        #: is comes through here.
        self._slots: dict[int, Slot] = {}
        self._planes: dict[Slot, Layer] = {}
        self._neighbours: dict[tuple[Slot, Side], Slot | None] = {}
        for layer in planes:
            for slot in layer.rects:
                self._planes[slot] = layer
                for pane in slot.panes:
                    if id(pane) in self._slots:
                        raise ValueError("%r is in two slots of one plan" % (pane,))
                    self._slots[id(pane)] = slot

    @property
    def ground(self) -> Layer:
        "The plane the panes are laid out on, empty when nothing is on it."
        layer = self.layers.get(GROUND)
        return Layer({}) if layer is None else layer

    # ------------------------------------------------------------------
    # What is on it.

    @property
    def slots(self) -> list[Slot]:
        "Every slot of every plane, back to front."
        return list(self.rects)

    @property
    def panes(self) -> list[Pane]:
        """
        Every pane on the plan, the hidden ones as well.

        This is what a plan *holds*. What a person sees is `shown`, and
        that is what the numbering counts.
        """
        return [pane for slot in self.rects for pane in slot.panes]

    @property
    def shown(self) -> list[Pane]:
        "The one pane of each slot a person can see."
        return [slot.shown for slot in self.rects]

    def at(self, point: Point) -> Slot | None:
        """
        The topmost slot that holds that cell, if one does.

        **Topmost, because this is what a person points at.** A higher
        plane draws over a lower one, so the slot a person can see at a
        cell is the one on the highest plane that covers it. A float
        nobody can click is not a float. Lillecarl/pymux#356.

        `Layer.at` is the other reading and stays exact: it answers
        within one plane, which is what every geometry service needs --
        "the one to my left" has an answer among rectangles that do not
        overlap and none among rectangles that do.
        """
        for layer in reversed(self.layers.values()):
            slot = layer.at(point)
            if slot is not None:
                return slot
        return None

    def slot_of(self, pane: Pane) -> Slot:
        """
        The slot this pane is in.

        A pane that is not on the plan is a fault in whatever asked,
        so this raises rather than answering `None`. Everything that
        wants a pane's rectangle comes through here, because a pane
        does not have one: its slot does.
        """
        try:
            return self._slots[id(pane)]
        except KeyError:
            raise KeyError("%r is not on this plan" % (pane,)) from None

    def rect_of(self, pane: Pane) -> Rect:
        "Where this pane is drawn, which is where its slot is."
        return self.rects[self.slot_of(pane)]

    def layer_of(self, slot: Slot) -> Layer:
        "The plane this slot is on."
        try:
            return self._planes[slot]
        except KeyError:
            raise KeyError("%r is not on this plan" % (slot,)) from None

    # ------------------------------------------------------------------
    # What is beside it.

    def neighbour(self, slot: Slot, side: Side) -> Slot | None:
        """
        The slot a person means by "the one to my left", and so on.

        **On that slot's own plane.** Across two planes the question
        has no answer: they may overlap, so "beyond this edge" stops
        being a place. Lillecarl/pymux#228.

        **Kept, because a plan is a value.** Every title bar asks three
        sides of its pane on every frame, and a plan that `same_as`
        the last one is kept across frames. Lillecarl/pymux#578.
        """
        # `get` and not `try`: a fresh plan misses every time, and a
        # raised KeyError costs more than the lookup it guards.
        key = (slot, side)
        beside = self._neighbours.get(key, _UNASKED)
        if beside is _UNASKED:
            beside = self._neighbours[key] = self.layer_of(slot).neighbour(slot, side)
        return cast("Slot | None", beside)

    def same_as(self, other: Plan) -> bool:
        """
        Whether the two plans put the same panes in the same places.

        Slots are new on every measure, so this compares what they
        hold: the panes, the one shown, and the rectangle.
        """
        if self.lines != other.lines or self.layers.keys() != other.layers.keys():
            return False
        for number, layer in self.layers.items():
            mine = layer.rects.items()
            theirs = other.layers[number].rects.items()
            if len(mine) != len(theirs):
                return False
            for (slot, rect), (other_slot, other_rect) in zip(mine, theirs, strict=True):
                if rect != other_rect or slot.showing != other_slot.showing or slot.panes != other_slot.panes:
                    return False
        return True

    def trace(self, origin: Point, angle: float) -> Slot | None:
        """
        The first slot of the ground plane a ray from that cell runs
        into. The ground plane, for the reason `at` gives.
        """
        return self.ground.trace(origin, angle)

    # ------------------------------------------------------------------
    # What reaches the screen.

    def drawn(self) -> dict[Slot, Rect]:
        """
        The slots worth drawing, in paint order.

        A slot the planes above cover whole is left out: nothing of it
        reaches the screen, so drawing it is work nobody sees. Carl:
        "ideally we'd walk this backwards as well so we don't bother
        computing things from lower planes that are entirely
        invisible".

        **Covered by the planes above together**, and not by any one
        rectangle of them. Two floating windows side by side hide a
        pane that neither of them hides alone.

        Nothing draws from this yet: `PlanContainer` still walks
        `rects`. Lillecarl/pymux#355.
        """
        over: list[Rect] = []
        kept: list[dict[Slot, Rect]] = []

        for layer in reversed(self.layers.values()):
            kept.append({slot: rect for slot, rect in layer.rects.items() if not covered(rect, over)})
            over.extend(layer.rects.values())

        return {slot: rect for held in reversed(kept) for slot, rect in held.items()}

    def __repr__(self) -> str:
        return "Plan(%r)" % (self.layers,)
