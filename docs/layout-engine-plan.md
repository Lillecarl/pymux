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

`Plane.place(slot, rect)` is meaningful on the bare plane and must not
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
3. **Things stay exactly where they are put, and nothing overlaps.**
   Carl: "Yes on Plane things stay exactly where you put them, the only
   thing we don't allow is overlapping". So the bare plane persists its
   rectangles between frames, and holes are allowed.
4. **A slot stacks, and nothing else.** More than one pane in a
   rectangle means tabbed: one visible, the rest hidden behind it.
   Splitting stays a structure that produces *more* rectangles. A niri
   column with two visible windows is two rectangles that `Strip` stacks
   vertically -- which is what pymux does today -- and a tabbed column
   is one slot holding two panes. **No recursion**: a slot never holds
   an internal layout.
5. **A slot is a real object, not a flag.** Carl: "tabstacking on
   rectangles is handled by an invisible wrapper element that owns 1 or
   more terminals". **So a rectangle belongs to a slot, and a slot owns
   one or more panes and shows one of them.**

   I had recorded the other spelling -- the plan keyed on panes with a
   visible set -- on my own recommendation, because it added no concept.
   The slot is better for two reasons I under-weighted:

   - **The overlap invariant loses a qualifier.** "No two slots
     overlap" needs no word about visibility, so the property test is
     about the plan's own shape and nothing else.
   - **Moving a stack is one operation on one object**, and tab
     stacking is exactly where a person expects to move the whole
     thing -- niri moves a tabbed column as a unit.

   The cost is real and worth knowing: **a pane no longer has a
   rectangle of its own.** Everything that asks where a pane is goes
   through its slot. That change is pervasive but mechanical.
6. **Every pane of a slot gets the slot's rectangle**, shown or not. A
   pty needs a size, and giving a hidden pane the size it will have
   means revealing it costs no `SIGWINCH` and no redraw.
7. **Neighbours: strict by default.** Among slots strictly beyond my
   edge in that direction whose perpendicular span overlaps mine, take
   the smallest gap; ties by largest overlap, then insertion
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
   **It counts the panes a person sees**, one per slot: Carl, on the
   tabbed case, "in a stack the visible pane is the only thing to be
   concerned with (at least for now)". So a stack is one number, a
   hidden pane has none, and a number does not move when somebody
   switches tab.
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

- **No two slots overlap.** No qualifier about visibility: a slot is
  the thing that has a rectangle, so this is about the plan's own shape.
- Every rectangle is at least one cell by one cell.
- Every slot owns at least one pane and shows exactly one of them.
- The plane's bounding box contains every rectangle and is no larger
  than it needs to be.
- `neighbour(A, d)` is strictly in direction `d` from A and its
  perpendicular band overlaps A's.
- `neighbour(A, d)` is never A.
- Walking one direction repeatedly terminates and visits no slot twice.
- Numbering is a permutation of the panes, and stable across a
  `measure` that changed nothing.

**Reachability is a tiling's promise, not the plane's.** "Every slot is
reachable from every other by some walk" was in this list and is false
for the bare plane: two rectangles set diagonally have no cardinal
neighbour at all, in either direction, which is decision 7 working as
agreed. It is true of any layout whose cuts run the whole way across,
because something on this side of a cut is always across from
something on that side. So it is held over `Divided` and `Strip` and
not over `Plane`. `test_a_diagonal_pair_are_not_neighbours` is the
counterexample, beside the symmetry one.

**A third plausible promise is false as well**: "a slot to the left of
another comes earlier in reading order". A full width pane between two
rows breaks it, and the tree does the same thing today, so the
numbering is right and the promise is wrong.
`test_reading_order_follows_the_splits_and_not_the_rows` holds the
shape.

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
    Slot     panes:   list[Pane]         -- one or more
             showing: int                -- which one a person sees
    Plan     rects:   dict[Slot, Rect]   -- a slot is what has a rectangle
             plane:   Rect               -- the bounding box
             order:   list[Pane]         -- numbering, over panes
    View     offset:  Point              -- plane coordinate at the
                                            client's top left
             size:    Size               -- the client's terminal
             marks:   dict[str, Point]   -- snapshot locations

`Plan` carries the services, so every subclass gets them free and none
of them reimplements one: `at(point)`, `slot_of(pane)`,
`neighbour(slot, direction)`, `trace(origin, angle)`,
`reading_order()`.

**Numbering is over panes, not slots**, because a pane number is what
`select-pane -t 1` takes and what a title bar draws. A slot contributes
**the one pane it shows**, so a stack is one number and a hidden pane
has none until it is shown. It keeps its rectangle either way, so its
pty already has the size it will be shown at.

    class Plane:
        "An unbounded plane, and views onto it. Usable as it stands."

        def measure(self, available: Size) -> Plan     # the plan it holds
        def place(self, slot, rect) -> None            # free: not for a constraint
        def adopt(self, plan, panes) -> None           # switching mode
        def open(self, pane, beside, near) -> None     # a new slot, or a new tab
        def close(self, pane) -> None
        def move(self, slot, direction) -> bool
        def resize(self, slot, dx, dy) -> bool
        def look_at(self, plan, view, focus) -> Point  # default: leave it, clamped
        def order(self, plan) -> list[Pane]            # default: insertion order

**The base is not abstract. It is the plane itself**, and every other
one is a constraint on it:

    Plane              anything, anywhere. Holds its rectangles
      Masonry          packs them against each other, and keeps it tidy
      Divided          an exact tiling of `available`, from fractions
      Strip            a row of columns; the plane may be wider

    Zoomed(inner)      a wrapper: one pane fills the view, and the
                       layout it wraps keeps its own state untouched

**Inheritance follows a persistent rule, never an arrangement.** That
is the line, and tmux draws it in the same place. A rule about how
space is shared or where a new pane goes lives for as long as the
window does, and that is a class: `Divided` and `Strip` differ that
way, and a dwm-style master and stack that keeps arranging itself
would be a third. An *arrangement* runs once and is then finished, and
that is a function.

**The `select-layout` presets are arrangements, not modes.**
`tmux/layout-set.c` says so in its own header -- "these are one-off and
generate a layout tree" -- and the code holds it up: `layout_set_select`
runs `arrange(w)` and stores `w->lastlayout`, which is read in exactly
two places, both of them a person asking for a preset again
(`select-layout` with no argument, and `-n`/`-p` cycling). A split, a
resize or a pane closing never re-applies one. `main-horizontal` reads
`main-pane-height` when it runs and never again. So `Divided` does not
remember which preset it is, and asking what `main-vertical` does when
a pane opens is a question tmux does not answer, because by then there
is no `main-vertical`.

A constraint overrides `measure` to recompute its rectangles from its
own parameters, so the stored plan is a cache it rewrites wholesale. The
bare plane is the one that *persists* rectangles between frames, because
a pane has to stay where a person put it. **That asymmetry is the only
one in the hierarchy, and it is deliberate.**

**`adopt` is what mode switching becomes.** It already happens twice
today, ad hoc: `Window.strip = True` wraps the root, and
`select_layout` rebuilds the tree. Making it a base-class method
generalises what is there.

## What tmux does, read out of tmux

Carl asked, before slice 4: check how tmux behaves in its modes, and
what "zoomed" means. Read at `578e07fc`.

**tmux keeps one thing: a tree of cells with absolute sizes.**
`layout_cell` is `LEFTRIGHT`, `TOPBOTTOM` or `WINDOWPANE` and carries
cells, not fractions. A window resize adjusts that tree in place by a
delta (`layout_resize` -> `layout_resize_adjust`), so **tmux
accumulates the rounding drift** that our fraction per pane exists to
avoid. Ours can be better here, and the price is that `resize-pane +1`
has to set the fraction from the cells it wants, not the other way
round -- which is what `switch_column_width` already does for a strip.

**tmux's layout is allowed to be bigger than the window.** The comment
in `layout_resize` says a window can be smaller than its layout and
"redrawing this is handled at a higher level", and `resize_window`
clamps the *window* up to the layout rather than the layout down.
`main-horizontal` even asks for `(n * (PANE_MINIMUM + 1)) - 1` columns
and grows the window to fit. So the awkward half of an unbounded plane
is already in tmux; a plane makes it ordinary instead.

**Zoomed means one pane fills the window, and it is a layout swap.**
`window_zoom` saves every pane's cell and the root
(`saved_layout_cell`, `saved_layout_root`), then calls
`layout_init(w, wp)` for a fresh one-pane layout; `window_unzoom` puts
them all back. Not full screen -- full *window*, so the status bar and
the chrome stay. Three details worth copying:

- It refuses when the window has one pane (`window_count_panes` == 1).
- A resize while zoomed unzooms, resizes the saved layout, and
  re-zooms (`resize.c`), so the layout underneath stays right.
- Moving the focus to a pane that is not visible unzooms
  (`window.c:721`).

**That is exactly `Zoomed(inner)`**, and it is why zoom is a wrapper
and not a flag. pymux's flag is what makes Lillecarl/pymux#215: a
zoomed strip is not a strip, because `_build_layout` tests
`window.zoom` before `window.strip`.

**tmux has floating panes now, and they are our floats.** A cell can
be `LAYOUT_CELL_FLOATING`; `layout_cell_is_tiled` gates every tiling
sum, so a float hangs off the root and no arithmetic counts it. The
window keeps a separate `z_index` list for the order, and a float
marked `PANE_FLOATOVERZOOM` is re-created over a zoomed pane and keeps
any move it made while it was there. A tiling, a list of floats and a
z-order, which is the shape agreed above.

Drawing is one generic container, `PlanContainer`, doing for two axes
what `ScrollableStrip` does for one: for each slot's shown pane, write to the
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

1. **`Rect`, `Slot`, `Plan`, the services, and the property tests.** Pure code,
   no wiring, testable alone. **Landed**: `pymux/plane.py` and
   `tests/test_the_plane.py`, with `every_promise_holds(plan)` for the
   slices after it to hold their own `measure` to.
2. **`Strip` emits a plan, and the title bars plus `select-pane -L|-R`
   read it.** The probe. **Landed**: `Strip.measure` in
   `pymux/strip.py`, and `layout.the_pane_beside` is the one question
   both ask. Two things a person sees changed, both in a strip: a
   direction key works before the first frame, and the pane named
   beside a stack is the one sharing most of the edge.
3. **`PlanContainer`, and `Strip` draws through it.** **Landed**:
   `pymux/plan_container.py`, `Strip.chrome`, `Strip.look_at`.
   `ScrollableStrip` is deleted and `strip.py` draws nothing.
   The layout draws the borders; a pane knows nothing about them.
4. **`Divided` emits a plan**, with the presets on top as functions,
   held to the existing suite. `_lay_out` in `strip.py` already does
   the walk it needs -- weights, both axes, a gap between children --
   so the work is the presets, `resize-pane`, and deleting the tree
   branch of `the_pane_beside`. `Zoomed(inner)` belongs here too: it
   is small, it closes Lillecarl/pymux#215, and it is the shape tmux
   uses.
5. **`View` per client, with an offset**, and the `window-size` policy
   as a real option.
6. **`Plane` on its own, then `Masonry`.**

Slices 1 to 3 are landed, and they were worth doing whether or not the
rest follows: the two-answers problem is gone for one layout, and the
promises every later layout is held to are written down.

**Where slice 4 starts.** `the_pane_beside` and `the_plan_of`
(`pymux/layout.py`) fork on `window.strip`, and the other branch is
the tree walk. `_create_split` and `SizedBox` are what `Divided`
replaces. `every_promise_holds(plan)` in `tests/test_the_plane.py` is
what holds it, and `tests/test_the_plan_and_the_frame.py` is the
pattern for keeping a new plan honest against the frame while both
exist.

## Popups, and floating windows

Carl asked, and the answer decides one word in the invariants: "of
course popups should be allowed on top of other rectangles ... it's
absolutely breaking if we can't popup overlays where we want them."

**Nothing here can stop one.** Three layers reach the screen, and a
plan is only the third:

1. Chrome floats: the status bar, the message toolbar, the title bars
   and the `list-keys` dialog. `Float`s over the whole layout,
   `Z_INDEX` 5 to 9 (`layout.py:113`).
2. The overlay pane: `display-popup -E`, a real pane with a pty.
   `Pymux.overlay_pane` (`main.py:557`) hangs off the session and is
   **not in the window tree**, so no plan has ever held it.
3. The tiled panes, which is what a plan holds.

So "no two slots overlap" is a rule about the tiling and not about the
screen, and it keeps its words.

**A popup is anchored to the screen and a floating window is anchored
to the plane.** They are two features, not one. A popup is centred on
the client and never scrolls, which is what it is for, and it stays
where it is. A floating window is a pane a person parks somewhere and
leaves there, so it moves with the view and can be dragged. Carl:
floating window support "everywhere" makes sense, so it belongs to
**`Plane` and not to `Masonry`**: every subclass gets it.

`Plan` therefore grows a second list when float mode is built --
ordered back to front, allowed to overlap, and not part of `rects`.
Slice 1 does not add the field, because an empty list nobody writes is
dead weight, and the module says where it goes.

## Out of scope, on purpose

- Overlapping panes **inside the tiling**. Floats are the section
  above, and they are a layer, not a relaxed rule.
- niri's numbered horizontal planes. **pymux already has them:**
  `Arrangement.windows` is a numbered set with one visible at a time,
  which is a niri workspace exactly. A subclass can make switching them
  feel like vertical movement with no new concept underneath.
- Recursive containers.
