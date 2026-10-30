"""
Where each pane of a window goes: the plan a window's layout makes for
the room a client gives it, the gaps and bars it keeps, the panes it
hides, and the sizes a person drags. Arithmetic over the arrangement;
nothing here draws.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

from prompt_toolkit.application import get_app
from prompt_toolkit.data_structures import Size

from pymux import arrangement

from .cut_shade import Hidden, hex_of, hidden_of
from .divided import Divided
from .plane import Plan, Rect, Side, bounding_box
from .strip import Strip
from .tiling import BORDER_WIDTH, Gaps
from .zoomed import Zoomed

if TYPE_CHECKING:
    from pymux.layout import LayoutManager
    from pymux.main import Pymux

__all__ = [
    "bar_below_is_drawn",
    "change_pane_size",
    "cut_backdrop",
    "cut_shade_of",
    "gaps_of",
    "hidden_of_pane",
    "layout_of",
    "pane_beside",
    "plan_of",
    "room_for_panes",
    "size_the_panes_of",
    "tell_pane_its_size",
    "write_sizes_into_weights",
]


def tell_pane_its_size(pane: arrangement.Pane, rect: Rect) -> None:
    """
    Give a pane the size of its rectangle.

    **The plan sizes a pane, and the drawing does not have to.** A
    pane that is drawn hears the same numbers again from
    prompt_toolkit, which costs nothing because neither the pty nor
    the screen acts on a size it already has. A pane that is *not*
    drawn -- scrolled out of the view, or behind another in a stack --
    hears them only here, and the program in it needs them: it writes
    for the screen it thinks it has. Lillecarl/pymux#224.
    """
    pane.terminal.set_size(rect.width, rect.height)


def layout_of(pymux: Pymux, window: arrangement.Window) -> Divided | Strip | Zoomed:
    """
    What says where the panes of this window are.

    One object, and everything asks it: the container that draws, the
    title bars, and the key that moves the focus. That is the whole of
    Lillecarl/pymux#217 -- "which pane is beside this one" had two
    answers, one read off the last frame and one walked out of the
    tree, and they did not agree.

    A strip is not a sixth layout: every other one divides the window
    between the panes, and a strip gives each column a width of its own
    and scrolls. Lillecarl/pymux#198.

    **Zoom wraps whichever of them the window is in**, and that is why
    it is a wrapper. A branch that tests `window.zoom` before
    `window.options.strip` makes a zoomed strip not a strip: the row stops
    existing for that frame instead of being covered.
    Lillecarl/pymux#215.

    The gaps are passed as a callable, because the gap between two
    stacked panes is two rows when a bar is drawn under a pane and one
    when it is not, and an option turns that on while the layout built
    here is still standing.
    """
    gaps = partial(gaps_of, pymux, window)
    inner = Strip(window, gaps) if window.options.strip else Divided(window, gaps)

    if window.zoom and window.active_pane is not None:
        return Zoomed(inner, window.active_pane)
    return inner


def bar_below_is_drawn(pymux: Pymux, window: arrangement.Window) -> bool:
    """
    Whether every pane of this window keeps a row under it.

    The bar under a pane names the pane above and the pane below it, so
    a window with no stack in it has nothing to put there and does not
    pay the row. A single pane and a plain row of panes look exactly as
    they did. Lillecarl/pymux#211.

    **A zoomed window has no stack on the screen**, whatever its tree
    holds: one pane covers everything, and nothing is above or below
    it. So it does not pay the row either.

    **Two places have to give the same answer**, or the rows drift: the
    row this client keeps under the whole layout, and the float that
    draws the bar. Both are this client's own drawing.

    The third is the gap between stacked panes, and that one is the
    plane's: `_the_plane_keeps_a_bar_below`. A full-screen client
    sharing a stacked window draws nothing in those gaps rather than
    closing them up, because the gap is geometry and a pane has one
    pty. Lillecarl/pymux#471.
    """
    return pymux.show_pane_status and not window.zoom and window.has_stack()


def _the_plane_keeps_a_bar_below(pymux: Pymux, window: arrangement.Window) -> bool:
    "The same rows, asked of every client watching. Lillecarl/pymux#471."
    return pymux.any_watcher_shows_pane_status(window) and not window.zoom and window.has_stack()


def gaps_of(pymux: Pymux, window: arrangement.Window) -> Gaps:
    """
    The cells this window leaves between the things in it.

    One for the border a column owns, and two rows between stacked
    panes when a pane draws a bar below it and the pane under it draws
    one above. `_create_split` leaves the same rows for the same
    reason. Lillecarl/pymux#211.

    **The plane's answer**, because a plan is one plan for every client
    watching. Lillecarl/pymux#471.
    """
    return Gaps(
        between_columns=BORDER_WIDTH,
        between_panes=2 if _the_plane_keeps_a_bar_below(pymux, window) else 1,
    )


def room_for_panes(pymux: Pymux, window: arrangement.Window) -> Size:
    """
    How much of the plane the panes get.

    The rows the chrome takes come off first, because a plan holds
    panes and the chrome draws in the gaps between them: one row for
    the bar above the top pane, and one for the bar below the bottom
    one where that is drawn. Lillecarl/pymux#217.

    **This is the size a plan is measured for**, and it is the plane's
    and not a client's. A client smaller than this sees part of the
    plan through a view of its own.

    So the rows come off for any client watching that draws the
    chrome, and never for the client asking: a full-screen client draws
    background where another one's titlebars go, rather than making a
    plan of its own and resizing everybody's programs.
    Lillecarl/pymux#471.
    """
    size = cast(Size, pymux.plane_size(window))

    rows = size.rows
    if pymux.any_watcher_shows_pane_status(window):
        rows -= 1
    if _the_plane_keeps_a_bar_below(pymux, window):
        rows -= 1

    return Size(rows=max(1, rows), columns=size.columns)


def plan_of(pymux: Pymux, window: arrangement.Window) -> Plan:
    """
    Where the panes of this window are, in cells.

    **The frame's own plan, when there is one.** A title bar is drawn
    inside the frame that the container measured, so the answer it
    wants is the plan already on the container -- and asking for it is
    the difference between one measurement per frame and one per
    question. A window of sixteen panes asks sixty-four of them (four
    sides each), and measuring each time cost seventeen times the whole
    frame: 950k bytecode instructions against 55k, paid on every
    frame. `tests/measure_frame.py` is where that number comes from.

    **Measured fresh when there is no frame to read**, which is what
    makes a direction key work before anything is drawn. That is the
    property slice 2 of Lillecarl/pymux#217 added, and it is held by
    `test_key_moves_focus_before_anything_is_drawn`.
    """
    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        # No client, so no frame and nowhere to keep an answer. A
        # command from the command line runs on a client like that.
        return layout_of(pymux, window).measure(room_for_panes(pymux, window))

    plans = manager.frame_plans
    if plans is not None and window in plans:
        return plans[window]

    size = room_for_panes(pymux, window)
    plan = _frame_plan(manager, window, size)
    if plan is None:
        plan = layout_of(pymux, window).measure(size)
    if plans is not None:
        plans[window] = plan
    return plan


def size_the_panes_of(pymux: Pymux, window: arrangement.Window) -> None:
    """
    Give every pane of this window the size the plan gives it.

    **A frame does this, and a window nobody watches gets no frame.**
    `PlanContainer.write_to_screen` tells each pane its rectangle, so
    an attached client's next frame is what makes a resize reach the
    programs. With nobody attached there is no next frame: the plane
    answered a new number and the ptys kept the old one, which is a
    size the program in the pane never hears about.

    Every pane of every slot, shown or not, for the reason
    `PlanContainer` gives: a hidden pane that already has the size it
    will be shown at is revealed without a resize.
    Lillecarl/pymux#459.
    """
    plan = plan_of(pymux, window)
    for slot, rect in plan.rects.items():
        for pane in slot.panes:
            tell_pane_its_size(cast(arrangement.Pane, pane), rect)


def hidden_of_pane(pymux: Pymux, pane: arrangement.Pane) -> Hidden | None:
    """
    How much of this pane the view this client has hides, on each side.

    **A cut pane is the one case where what a person sees is not what
    the program wrote**, and the cells give nothing away: a pane whose
    program wrote nothing past the cut looks exactly like a pane that
    ends there. So the cut edge is shaded, and this is the question.
    Lillecarl/pymux#222, Lillecarl/pymux#616.

    It reads the frame that is being drawn, where the view is, glide
    and all. Before the first frame, and for a pane of another window,
    the answer is None: nothing is drawn, so nothing is cut.
    """
    try:
        manager = pymux.get_client_state().layout_manager
    except ValueError:
        return None

    container = manager.pane_container()
    if container is None or container.plan is None:
        return None

    try:
        rect = container.plan.rect_of(pane)
    except KeyError:
        return None

    return hidden_of(container.view.shown_rect, rect)


def cut_shade_of(pymux: Pymux) -> tuple[int, float]:
    "How many cells the shade at a cut fades over, and how strong it is, for the client drawn."
    options = pymux.current_session.options
    return options.cut_shade_width, options.cut_shade_strength / 100


def cut_backdrop(pymux: Pymux) -> str | None:
    """
    What a cell the program left at the default background really shows.

    **Not always the theme's colour.** With `paint-screen` off the cell
    shows the terminal's own background, which the client asked its
    terminal for (`OSC 11`, Lillecarl/pymux#223), so a shade picked
    against the theme's would lift one terminal and darken the next.
    Lillecarl/pymux#352. With `paint-screen` on, the cell's own style
    already names the theme's, and a terminal that answered nothing
    leaves the theme's as the best guess.
    """
    if not pymux.current_session.options.paint_screen:
        try:
            background = pymux.get_client_state().default_colors.background
        except ValueError, AttributeError:
            background = None
        if background is not None:
            return background.hex
    return hex_of(get_app().renderer.style.get_attrs_for_style_str("class:painted").bgcolor or "")


def _frame_plan(manager: LayoutManager, window: arrangement.Window, size: Size) -> Plan | None:
    """
    The plan the container of this frame measured, when it is still the
    answer.

    A title bar is drawn inside that frame, so this is the plan it is
    being drawn against, and reading it means the frame is measured
    once rather than once per pane.

    Two things have to hold, and each rules out a plan that would lie:

    - The container is the one for *this* window, and it measured for
      this size. A client that resized between two frames has a plan
      of the size it was.
    - The arrangement has not changed shape since. That one is free:
      `DynamicBody` rebuilds its body when the invalidation hash
      moves, and a fresh container has drawn nothing, so a pane
      opening, closing or zooming falls through to a fresh
      measurement.
    """
    container = manager.pane_container()
    if container is None or container.measured_for != size:
        return None

    if getattr(container.layout, "window", None) is not window:
        return None

    return container.plan


def write_sizes_into_weights(pymux: Pymux, window: arrangement.Window) -> None:
    """
    Write the size each thing has now into the weight that decides it.

    **This is what makes `resize-pane -U 1` mean one row.** A weight is
    a share of a split and not a number of cells, so a delta of one
    added to a share of one means nothing on a screen. Measure first,
    write the cells each child holds into its weight, and then a delta
    of one is one cell -- the weights add up to the room, so each
    child's share of it comes out as the number itself.

    The plan decides the cells, and this is the one place that
    writes a weight back. A render that writes them on every frame
    leaves the arrangement holding four kinds of geometry at once.

    tmux does the opposite and pays for it: `layout_resize_adjust`
    keeps absolute cells and moves them by a delta, so its rounding
    drifts over repeated resizes.
    """
    plan = plan_of(pymux, window)

    for split in window.splits:
        sideways = isinstance(split, arrangement.VSplit)

        for child in split:
            held = [plan.rect_of(pane) for pane in arrangement.panes_of(child)]
            if not held:
                continue

            box = bounding_box(held)
            split.set_weight(child, box.width if sideways else box.height)


def change_pane_size(
    pymux: Pymux,
    window: arrangement.Window,
    pane: arrangement.Pane,
    up: int = 0,
    right: int = 0,
    down: int = 0,
    left: int = 0,
) -> None:
    """
    Make one pane bigger or smaller, by that many cells on each side.

    **One door**, because a resize is two steps and the first one is
    easy to forget: the weights have to say what the panes measure now
    before a delta means anything. `resize-pane` and a program asking
    for a size both come through here.
    """
    write_sizes_into_weights(pymux, window)
    window.change_size_for_pane(pane, up=up, right=right, down=down, left=left)


def pane_beside(
    pymux: Pymux, window: arrangement.Window, pane: arrangement.Pane, side: Side
) -> arrangement.Pane | None:
    """
    The pane on one side of this one. **One answer, for one window.**

    Two mechanisms answered this and they did not agree.
    `select-pane -L` read where the panes were drawn last frame, and a
    title bar cannot use that because it is drawn *during* a frame, so
    it walked the arrangement's tree instead. The tree named the top of
    a stack beside us; the frame named whichever pane the active one's
    own row ran into. Lillecarl/pymux#217.

    The plan answers, which is neither: it names the slot that shares
    most of our edge, and then the pane that slot shows.

    **A zoomed pane has nothing beside it**, because it covers the
    window. That is what both mechanisms already did, and saying it
    once here keeps them saying it.
    """
    if window.zoom:
        return None

    plan = plan_of(pymux, window)

    try:
        slot = plan.slot_of(pane)
    except KeyError:
        # A pane that is not in this window's plan: it closed, or it
        # belongs to another window. A bar is drawn on every frame, so
        # it may not be the thing that finds that out.
        return None

    beside = plan.neighbour(slot, side)
    if beside is None:
        return None

    return cast(arrangement.Pane, beside.shown)
