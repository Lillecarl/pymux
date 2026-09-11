"""
The walk from a tree of splits to a plan, and the gaps it leaves.

Two layouts share it. `Divided` divides the view between the panes,
which is what every layout pymux had before the strip does, and `Strip`
gives each column a width of its own and lets the row run past the
edge. Both are trees of splits underneath, so both divide a rectangle
between the children of a split, and both leave a gap between one child
and the next for the chrome that goes there. Lillecarl/pymux#217.

**A gap is a hole in the plan, not a thinner pane.** A layout says what
fills it (`Line`), and `PlanContainer` paints it. A pane knows nothing
about borders.

It still reads `pymux.arrangement` for the tree and the weights. The
plan file says the tree becomes these layouts' own state later; today
it is the arrangement's, and this measures it.
"""

from typing import NamedTuple

from . import arrangement
from .plane import Line, Rect, Slot, bounding_box

__all__ = ["BORDER_WIDTH", "Gaps", "lay_out", "shares"]

#: The cell a column keeps for the border on its right. It lives here
#: because a column's width is measured around it.
BORDER_WIDTH = 1

#: What a layout draws in the gaps it leaves. The focused pane draws
#: its own heavier border over the top of these, as a float.
BORDER_VERTICAL = "│"
BORDER_HORIZONTAL = "─"


class Gaps(NamedTuple):
    """
    The cells a layout leaves between the things it lays out.

    Both are chrome, so a plan puts a hole there rather than giving
    the cells to a pane. `layout.py` decides the numbers, because it
    is what draws in them: two rows between stacked panes when the bar
    below a pane is drawn, and one when it is not.
    """

    between_columns: int = BORDER_WIDTH
    between_panes: int = 1


def lay_out(
    item, rect: Rect, gaps: Gaps, into: list, lines: list | None = None
) -> Rect:
    """
    Put every pane of this item on the plane, inside that rectangle.

    A pane is one slot. A split divides its rectangle between its
    children, leaving a gap between each pair for the chrome that goes
    there.

    `lines` collects the chrome, when a caller wants it: one line for
    each gap, running the whole way across the split that left it. That
    is what a border between two panes is, and drawing it here is what
    keeps it where the gap is.

    **It answers with the room it really took**, which is the rectangle
    it was given or more. Every pane keeps at least one cell
    (`shares`), so a split of more panes than cells runs past the end
    of its rectangle, and the next child has to start past *that* and
    not where the arithmetic said. Without it a window shrunk far
    enough lays one pane on top of another, and no two slots
    overlapping is the promise everything else rests on. The plane is
    unbounded, so running past the end is allowed and a view is what
    cuts it.
    """
    if not isinstance(item, (arrangement.VSplit, arrangement.HSplit)):
        into.append((Slot(item), rect))
        return rect

    sideways = isinstance(item, arrangement.VSplit)
    gap = gaps.between_columns if sideways else gaps.between_panes
    room = (rect.width if sideways else rect.height) - gap * (len(item) - 1)
    parts = shares(room, [item.weights[child] for child in item])

    at = rect.x if sideways else rect.y
    taken = [rect]

    for place, (child, share) in enumerate(zip(item, parts)):
        if sideways:
            used = lay_out(
                child, Rect(at, rect.y, share, rect.height), gaps, into, lines
            )
            at = max(at + share, used.right)
        else:
            used = lay_out(
                child, Rect(rect.x, at, rect.width, share), gaps, into, lines
            )
            at = max(at + share, used.bottom)

        taken.append(used)

        # The gap after this child, which the next one starts past.
        # The last child has none: a split ends where its rectangle
        # ends. A strip is the layout that keeps one there anyway, and
        # it says so itself.
        if lines is not None and place < len(item) - 1:
            if sideways:
                lines.append(Line(Rect(at, rect.y, gap, rect.height), BORDER_VERTICAL))
            else:
                lines.append(Line(Rect(rect.x, at, rect.width, gap), BORDER_HORIZONTAL))

        at += gap

    return bounding_box(taken)


def shares(total: int, weights: list[int]) -> list[int]:
    """
    How many cells each child of a split takes.

    A weight is a share of the whole, so this divides in proportion and
    never in absolute cells. That is the one thing tmux does not do:
    `layout_resize_adjust` moves its cells by a delta, so its rounding
    drifts over repeated resizes and ours cannot.
    `layout.write_sizes_into_weights` is what makes `resize-pane +1`
    mean one cell, by writing the size a person asked for into the
    weights.

    The cells that do not divide evenly go to the children the
    division shortchanged most, and a tie goes to the one nearer the
    start.

    **Every child gets at least one cell**, because a rectangle with no
    cells is a rectangle nothing can be drawn in. A stack too tall for
    the room therefore runs past it, which the plane allows: it is
    unbounded, and a view is what is bounded.
    """
    count = len(weights)
    total = max(total, count)
    whole = sum(weights) or count

    exact = [total * weight / whole for weight in weights]
    parts = [max(1, int(share)) for share in exact]

    spare = total - sum(parts)
    if spare > 0:
        wanting = sorted(range(count), key=lambda i: (int(exact[i]) - exact[i], i))
        for i in wanting[:spare]:
            parts[i] += 1

    return parts
