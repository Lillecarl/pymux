"""
A bar in three parts: this pane's title, and its neighbours' names.

      1 ◂ vim          bash          less ▸

The middle is the pane's own title, centred over the pane. The two
edges name the pane to the left and the pane to the right, and each one
is absent when there is no such pane. Lillecarl/pymux#207.

**This is the navigation of a strip.** A strip runs past the edge of
the screen, so a person has to know what is out there. The answer used
to be a peek: two cells of the next column showed at the edge, which
says that something is there and nothing about what. A name says what.
The peek is gone, so the bar is now the only answer.
Lillecarl/pymux#198, Lillecarl/pymux#209.

**The width is why this is a control and not three windows.** Three
`Window`s in a `VSplit` would each need a width before any of them
knows how wide the bar is, so the middle would be centred over
whatever is left rather than over the pane, and a name could only be
cut to a width nobody had measured. `UIControl.create_content` is
handed the real width, which is where all three questions are
answered at once.

**The pane's own number is a fourth part, and it is not an edge.** It
sits at the far left, it is four cells, and it may not be cut: a
number with a letter missing is another pane's number. So it is taken
off the bar before the edges are given their share. Counting it as
part of the left edge cost that edge four of its nine cells, and the
name of the pane on the left came out as `alp…` while the one on the
right fitted whole.

The order the parts give way in is the order they matter:

- The number is never cut.
- The pane's own title is the middle and it is never dropped.
- A neighbour takes at most a quarter of what is left, and is cut to
  fit.
- Below a handful of cells the neighbours go, and the title stays.
"""

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.utils import get_cwidth

__all__ = ["PaneTitleBar", "lay_out_the_bar"]

#: What one edge may take of the bar: one part in this many. A quarter
#: each leaves half the bar for the title, which is what has to be
#: readable.
EDGE_SHARE = 4

#: Under this many cells an edge would hold a letter and an ellipsis,
#: which names nothing. A narrow column drops its neighbours and keeps
#: its own title.
NARROWEST_EDGE = 4

#: What a name that was cut ends with.
ELLIPSIS = "…"


class PaneTitleBar(UIControl):
    """
    One row: the number, the left name, the title, the right name.

    Each part is a callable that returns fragments, because all of them
    change under the bar -- a title, a neighbour that comes and goes.

    :param get_number: Fragments for the far left, never cut.
    :param get_left: Fragments for the left edge. They are cut from the
        right, so what is furthest left survives.
    :param get_middle: Fragments for the middle. Never dropped.
    :param get_right: Fragments for the right edge, cut the same way.
    """

    def __init__(self, get_number, get_left, get_middle, get_right) -> None:
        self.get_number = get_number
        self.get_left = get_left
        self.get_middle = get_middle
        self.get_right = get_right

    def create_content(self, width: int, height: int) -> UIContent:
        line = lay_out_the_bar(
            self.get_number(),
            self.get_left(),
            self.get_middle(),
            self.get_right(),
            width,
        )
        return UIContent(get_line=lambda i: line, line_count=1, show_cursor=False)


def lay_out_the_bar(
    number: StyleAndTextTuples,
    left: StyleAndTextTuples,
    middle: StyleAndTextTuples,
    right: StyleAndTextTuples,
    width: int,
) -> StyleAndTextTuples:
    """
    The parts across `width` cells, as one row of fragments.

    The middle is centred over the whole bar, and moves aside only
    when it would otherwise run into an edge. So a title stays where a
    person expects it while a neighbour's name grows or shrinks under
    it.
    """
    if width <= 0:
        return []

    number = _cut_to(number, width)

    budget = (width - fragment_list_width(number)) // EDGE_SHARE
    if budget < NARROWEST_EDGE:
        left = right = []
        budget = 0

    left = number + _cut_to(left, budget)
    right = _cut_to(right, budget)

    left_width = fragment_list_width(left)
    right_width = fragment_list_width(right)

    middle = _cut_to(middle, width - left_width - right_width)
    middle_width = fragment_list_width(middle)

    # Centred over the bar, then pushed off the edges it would land on.
    start = (width - middle_width) // 2
    start = max(left_width, min(start, width - right_width - middle_width))

    return (
        left
        + [("", " " * (start - left_width))]
        + middle
        + [("", " " * (width - start - middle_width - right_width))]
        + right
    )


def _cut_to(fragments: StyleAndTextTuples, width: int) -> StyleAndTextTuples:
    """
    The start of these fragments, at most `width` cells wide.

    A name that had to be cut ends in an ellipsis, so that a person can
    see that there is more of it. The ellipsis takes the style of the
    fragment it cuts, so a cut name is still styled as one name.
    """
    if fragment_list_width(fragments) <= width:
        return list(fragments)
    if width <= 0:
        return []

    room = width - get_cwidth(ELLIPSIS)
    kept: StyleAndTextTuples = []
    style = ""
    taken = 0
    full = False

    for fragment in fragments:
        if full:
            break

        style = fragment[0]
        text = ""

        for character in fragment[1]:
            cells = get_cwidth(character)
            if taken + cells > room:
                # A wide character that does not fit ends the cut. The
                # next one might be narrow enough, and taking it would
                # drop the character between them.
                full = True
                break

            text += character
            taken += cells

        if text:
            kept.append((style, text))

    return kept + [(style, ELLIPSIS)]
