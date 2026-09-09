"""
Zoom: one pane fills the window, and nothing else is laid out.

**It is a wrapper around a layout, not a flag inside one.** `Zoomed`
holds the layout the window would have, answers with one pane over the
whole view, and hands the window back untouched when it goes. tmux does
the same thing in its own words: `window_zoom` saves every pane's cell
and the root of the tree, calls `layout_init` for a fresh one-pane
layout, and `window_unzoom` puts the saved one back.

The flag is what made Lillecarl/pymux#215. `_build_layout` tested
`window.zoom` before `window.strip`, so a zoomed strip was not a strip
at all: the row, the scrolling and the column widths stopped existing
for that frame rather than being covered by the pane on top of them. A
wrapper cannot do that, because what it wraps is still there.

**Zoomed means the whole window, not the whole screen.** tmux keeps its
status bar while a pane is zoomed and so do we, and the pane keeps the
row its title bar hangs in.
"""

from prompt_toolkit.data_structures import Point, Size

from .plane import Line, Pane, Plan, Rect, Slot

__all__ = ["Zoomed"]


class Zoomed:
    """
    The layout a window has while one of its panes fills it.

    :param inner: The layout underneath, which measures nothing while
        this stands. It is held because it is what comes back: a
        person unzooms into the row or the tiling they left, with the
        widths and the weights they gave it.
    :param pane: The one pane a person sees.
    """

    def __init__(self, inner, pane: "Pane") -> None:
        self.inner = inner
        self.pane = pane

    def __repr__(self) -> str:
        return "Zoomed(%r, %r)" % (self.inner, self.pane)

    def measure(self, available: Size) -> Plan:
        "One slot, the size of the view."
        return Plan(
            {
                Slot(self.pane): Rect(
                    x=0, y=0, width=available.columns, height=available.rows
                )
            }
        )

    def chrome(self, plan: Plan) -> list[Line]:
        """
        Nothing.

        A border fills a gap between two panes, and a zoomed pane has
        no neighbour to be divided from.
        """
        return []

    def look_at(
        self, plan: Plan, offset: Point, size: Size, focus: "Pane | None"
    ) -> Point:
        "The origin: the pane is exactly the view."
        return Point(x=0, y=0)
