# The layout engine: the plan we agreed

**This file is temporary.** It exists so that the design survives a lost
session, and it is deleted when the work lands. Lillecarl/pymux#217 holds
the same design and the argument for it; this is the short form a person
picks up cold.

Agreed with Carl over four rounds on 2026-09-09.

## The goal

One layer that answers "where is every pane", so that a layout
experiment costs a class and nothing else. Carl: "We want a **rock
solid** underlying architecture that we can easily try out new layout
experiments in ... One base class gives us all the primitives and then
the rules/behavior goes into subclasses."

## Why, in one paragraph

Four places decide geometry today: the tree and widths in
`arrangement.Window`, the weights in `layout._create_split`, the exact
widths in `layout._create_strip`, and the render writing real sizes back
through `report_write_position_callback`. The clearest symptom is that
**"which pane is beside this one" has two answers** -- `select-pane -L`
reads where panes were drawn last frame, and a title bar cannot use that
because it is drawn *during* a frame, so `Window.pane_to_the_left` walks
the tree instead. The second symptom is that every general feature has
been paid for twice, once for divided layouts and once for the strip:
Lillecarl/pymux#206, #198, #210, #211.

## The name

**`Plane`, and never `Layout`.** The building block is *a plane that
supports views*, and that is what the base class is called. Carl:
"the masonry name suggests things needs to stay next to eachother,
that's pretty much what managedmasonry would be doing, the real base is
more practically an 'unbounded plane'". So `Masonry` names the subclass
that actually packs rectangles against each other, which is what the
word means.

`Layout` is taken twice over. prompt_toolkit owns
`prompt_toolkit.layout.Layout` -- the container tree, the focus,
`current_window`, `walk_through_modal_area` -- and pymux has `layout.py`
and `LayoutManager` besides. Two different jobs: prompt_toolkit's
`Layout` answers *what has focus*, and ours answers *where things are*.
Both keep existing, and they meet in one place, the container that draws
a plan.

## One rule that must not be broken

**A constrained subclass never lets an unconstrained mutation through.**

`Plane.place(pane, rect)` is meaningful on the bare plane and must not
be reachable on `Divided`, or somebody calls it and quietly breaks the
tiling. Either the mutators are private and each subclass exposes its
own, or a constraint refuses them.

That is the whole of it, and it is worth recording that a longer rule
was considered and dropped. I argued for a while that each subclass
should own a private model and merely *project* a plan, on the grounds
that `Divided` needs a tree and inheriting a rect store would give it
two models. Two things killed that argument:

- **i3 keeps a tree for what we have excluded.** Its tree exists for
  nested containers and recursive tabbed and stacked modes. Decision 4
  below rules both out, and the tree's main job goes with them.
- **Adjacency is only ambiguous on arbitrary rectangles.** `Divided`
  guarantees an exact tiling, and on a tiling adjacency is exact: my
  right-hand neighbours are the rectangles whose left edge equals my
  right edge. Resize needs nothing more.

The one job a tree still did is **proportional rescaling**: when a
client goes from 80 columns to 100, a tiling must redistribute, and
scaling rectangles accumulates rounding drift over repeated resizes. So
`Divided` keeps a **fraction per pane** as its own constraint
parameter, and derives cells from fractions on every measure. Cells stay
the truth in the plan; the fraction is the constraint's business, which
is where the behaviour belongs.

## The decisions, all locked

1. **Coordinates** are integer terminal cells on a plane. `x` and `y`
   may be negative: the plane is unbounded in every direction.
2. **Free rectangles, no lattice.** Edges need not align with a shared
   set of boundaries. A lattice buys nothing for neighbour queries --
   ray casting is exact and cheap on free rectangles, which is what sway
   does. What a lattice buys is automatic tiling under resize, and
   `Divided` gets that from its own fractions and from the fact that
   adjacency on a tiling is exact.
3. **No two *visible* panes overlap.** Holes are allowed.
4. **A container stacks, and nothing else.** More than one pane in a
   rectangle means tabbed: one visible, the rest hidden behind it.
   Splitting stays a structure that produces *more* rectangles. A niri
   column with two visible windows is two rectangles that `Strip` stacks
   vertically -- which is what pymux does today -- and a tabbed column
   is one rectangle holding two panes. **No recursion**: a container
   never holds an internal layout.
5. **Visibility flag, not a slot object.** The plan keys on panes and
   carries which are visible. This adds no concept to the base; a
   subclass that wants group moves may model a slot privately.
6. **A hidden pane gets the whole rectangle.** A pty needs a size, and
   giving a hidden pane the size it will have means revealing it costs
   no `SIGWINCH` and no redraw.
7. **Neighbours: strict by default.** Among visible rectangles strictly
   beyond my edge in that direction whose perpendicular span overlaps
   mine, take the smallest gap; ties by largest overlap, then insertion
   order. If nothing overlaps my span, the answer is nothing -- no
   diagonal jump. tmux is strict here and it is the less surprising
   behaviour.
8. **An angled ray is allowed, just not the default.** Carl: "tracing
   diagonally (even on floating point angles) shouldn't be 'blocked',
   just not the default (integral tracing is cheaper)." So the API is a
   general `trace(origin, angle)` with the four cardinal directions as a
   separate cheap path that tests band overlap instead of casting.
9. **Numbering is insertion order in the base, overridable.** `Divided`
   and `Strip` override with reading order (Lillecarl/pymux#210).
   Numbers therefore change on a *mode* switch, and `display-panes` on
   a bare plane shows numbers that are not in reading order. Carl has
   made that trade for stability while rectangles move.
10. **Views are attached to planes.** A plane has a size; each view is
    one client's terminal. A view smaller than the plane sees part of
    it. **A pane has one pty, so it has one size however many clients
    look at it** -- the plan is shared, never per client.
11. **The window-size policy belongs to bounded layouts.**
    `smallest`, `largest`, `latest` as options and `manual` as a
    command, spelled as tmux spells them. Today pymux hard-codes
    `smallest` (`main.py:696`) with no way to say otherwise, which is a
    gap regardless of this work. `window-size largest` falls out for
    free once views are separate from planes, and better than tmux's,
    because a client too small to see the whole plane can move its view
    instead of being stuck at the top left.
12. **Snapshot locations are saved view offsets.** A dictionary of named
    points per view. They work in every subclass, including the ones
    that do not scroll.

## The invariants, as property tests

This is what makes it solid. Not the base class -- the invariants every
subclass is held to. For any model and any available size:

- No two visible rectangles overlap.
- Every rectangle is at least one cell by one cell.
- The plane's bounding box contains every rectangle and is no larger
  than it needs to be.
- `neighbour(A, d)` is strictly in direction `d` from A and its
  perpendicular band overlaps A's.
- `neighbour(A, d)` is never A.
- Walking one direction repeatedly terminates and visits no rectangle
  twice.
- Every visible rectangle is reachable from every other by some walk.
- Numbering is a permutation of the panes, and stable across a
  `measure` that changed nothing.

**The symmetry invariant is false and must not be written.** "If B is
right of A then A is left of B" fails for any layout with variable-size
panes, including the ones pymux has today:

    +-----+-----------+
    |  A  |           |
    +-----+     B     |
    |     |           |
    |  D  |           |
    +-----+-----------+

`right(A)` is B and `right(D)` is B, and `left(B)` can only be one of
them.

hypothesis is already in the check inputs.

## The shape

Three data types and one base class.

    Rect     a cell rectangle on the plane; x and y may be negative
    Plan     rects: dict[Pane, Rect]
             visible: set[Pane]          -- at most one per rectangle
             plane: Rect                 -- the bounding box
             order: list[Pane]           -- numbering
    View     offset: Point               -- plane coordinate at the
                                            client's top left
             size: Size                  -- the client's terminal
             marks: dict[str, Point]     -- snapshot locations

`Plan` carries the services, so every subclass gets them free and none
of them reimplements one: `at(point)`, `neighbour(pane, direction)`,
`trace(origin, angle)`, `reading_order()`.

    class Plane:
        "An unbounded plane, and views onto it. Usable as it stands."

        def measure(self, available: Size) -> Plan     # the plan it holds
        def place(self, pane, rect) -> None            # free: not for a constraint
        def adopt(self, plan, panes) -> None           # switching mode
        def open(self, pane, beside, near) -> None
        def close(self, pane) -> None
        def move(self, pane, direction) -> bool
        def resize(self, pane, dx, dy) -> bool
        def look_at(self, plan, view, focus) -> Point  # default: leave it, clamped
        def order(self, plan) -> list[Pane]            # default: insertion order

**The base is not abstract. It is the plane itself**, and every other
one is a constraint on it:

    Plane              anything, anywhere. Holds its rectangles
      Masonry          packs them against each other, and keeps it tidy
      Divided          an exact tiling of `available`, from fractions
      Strip            a row of columns; the plane may be wider

The five `select-layout` presets are configurations of `Divided`, not
subclasses.

A constraint overrides `measure` to recompute its rectangles from its
own parameters, so the stored plan is a cache it rewrites wholesale. The
bare plane is the one that *persists* rectangles between frames, because
a pane has to stay where a person put it. **That asymmetry is the only
one in the hierarchy, and it is deliberate.**

**`adopt` is what mode switching becomes.** It already happens twice
today, ad hoc: `Window.strip = True` wraps the root, and
`select_layout` rebuilds the tree. Making it a base-class method
generalises what is there.

Drawing is one generic container, `PlanContainer`, doing for two axes
what `ScrollableStrip` does for one: for each visible pane, write to the
screen at `rect - view.offset`. `pymux/pymux/strip.py` collapses into
`Strip.look_at` plus that container.

## Why this is cheap on the drawing side

**The unbounded plane already exists one layer down.**
`Screen.data_buffer` is a sparse dictionary of rows of cells and the
renderer only reads the rectangle it is about to show, so drawing at a
negative offset clips for free. `ScrollableStrip` is 260 lines and
needed no change to prompt_toolkit. We are not fighting the drawing. We
are fighting the *measuring*: `preferred_width`/`preferred_height` and
the divide-by-weight pass, which assume the children fit.

Two smaller rules that follow:

- **The cursor** is hidden while the focused pane is out of view. The
  terminal has one cursor and `Screen.cursor_positions` places it from
  the focused pane. Lillecarl/pymux#201 made exactly this decision
  inside `ScrollablePane`.
- **The layout cache gets simpler.** Today the container tree encodes
  geometry as weights, so any geometry change rebuilds it. Under a plan
  the tree is one container per pane positioned per frame, so it rebuilds
  only when the *set* of panes changes.

## The cost, honestly

- **`resize-pane` inverts.** Today the render tells the arrangement how
  big things really are, so `+1` means exactly one row. Under a plan the
  arrangement decides and the render obeys. Simpler, and it touches
  every resize path.
- The `SizedBox` weight machinery goes.
- Most of `arrangement.py` is not deleted, it is *classified*: the tree,
  splits, presets, `consume_or_expel` and `move_column` become `Divided`
  and `Strip` internals.
- Chrome can keep using `Float` on day one.

## The slices, in order

1. **`Rect`, `Plan`, the services, and the property tests.** Pure code,
   no wiring, testable alone.
2. **`Strip` emits a plan, and the title bars plus `select-pane -L|-R`
   read it.** The probe. `Strip` already computes every number it needs
   (`width_of`, `_where_the_column_is`), and the title bar is where the
   two-answers problem is worst. If projection works on the hardest
   layout we already have, it works.
3. **`PlanContainer`, and `Strip` draws through it.** `strip.py` shrinks.
4. **`Divided` emits a plan**, with the five presets on top, held to the
   existing suite.
5. **`View` per client, with an offset**, and the `window-size` policy
   as a real option.
6. **`Plane` on its own, then `Masonry`.**

Slices 1 to 3 are worth doing whether or not the rest follows, because
they delete the two-answers problem for one layout.

## Out of scope, on purpose

- Overlapping panes as a feature. A floating pane is a different thing,
  and `display-popup` is already that.
- niri's numbered horizontal planes. **pymux already has them:**
  `Arrangement.windows` is a numbered set with one visible at a time,
  which is a niri workspace exactly. A subclass can make switching them
  feel like vertical movement with no new concept underneath.
- Recursive containers.
