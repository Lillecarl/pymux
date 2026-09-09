"""
Which pane is to the left of this one, and which is to the right.

A title bar names its neighbours, so that a person can see what is out
there without it being drawn. Lillecarl/pymux#207. The same answer
moves the focus, which is the point of Lillecarl/pymux#217: a key and a
bar that name different panes are two bugs waiting.

**The plan answers, and it is the only thing that does.** Two
mechanisms used to. `select-pane -L` read where the panes were drawn
last frame and stepped one cell past an edge, which is right for a key
press and impossible for a title bar, because a bar is drawn *during* a
frame and on the first frame there are no positions at all. So a bar
walked the arrangement's tree instead, and the two disagreed beside a
stack.

The rule the tests below hold to is one rule, and it is about
rectangles rather than about splits: **the neighbour is the slot beyond
this edge whose band overlaps ours by the most.** A tie goes to the one
laid out first. What the tree used to say -- a pane in a stack takes
the stack's neighbours, a column on our left gives its rightmost pane
-- falls out of that, because those are the panes that share the edge.
"""

from prompt_toolkit.data_structures import Size

from pymux.arrangement import HSplit, Pane, VSplit, Window
from pymux.divided import Divided
from pymux.plane import Side
from pymux.strip import Strip

#: A window big enough that every pane of these shapes has cells.
SIZE = Size(rows=24, columns=80)


class _Fake:
    "Enough of a pane for the arrangement to hold it."


def a_pane():
    return Pane(terminal=_Fake())


def a_window(root):
    "A window laid out exactly this way, however that was reached."
    window = Window()
    window.root = root
    return window


def beside(window, pane, side: Side, layout=Divided, size=SIZE):
    "The pane on one side of this one, as the layout sees it."
    plan = layout(window).measure(size)
    slot = plan.neighbour(plan.slot_of(pane), side)
    return None if slot is None else slot.shown


def a_row(how_many):
    "A row of panes side by side, and the panes in order."
    panes = [a_pane() for _ in range(how_many)]
    return a_window(VSplit(panes)), panes


# ----------------------------------------------------------------------
# A row of panes.


def test_the_middle_of_a_row_has_a_pane_on_each_side():
    window, panes = a_row(3)

    assert beside(window, panes[1], Side.LEFT) is panes[0]
    assert beside(window, panes[1], Side.RIGHT) is panes[2]


def test_the_ends_of_a_row_have_one_side_each():
    window, panes = a_row(3)

    assert beside(window, panes[0], Side.LEFT) is None
    assert beside(window, panes[0], Side.RIGHT) is panes[1]

    assert beside(window, panes[2], Side.LEFT) is panes[1]
    assert beside(window, panes[2], Side.RIGHT) is None


def test_a_lone_pane_has_neither():
    window, panes = a_row(1)

    assert beside(window, panes[0], Side.LEFT) is None
    assert beside(window, panes[0], Side.RIGHT) is None


def test_a_pane_that_is_the_whole_window_has_neither():
    "A window with no split at all, which is what a new one is."
    window = Window()
    window.add_pane(a_pane())

    assert beside(window, window.active_pane, Side.LEFT) is None
    assert beside(window, window.active_pane, Side.RIGHT) is None


# ----------------------------------------------------------------------
# A column that is a stack of panes, which is what a niri column is.


def test_a_pane_in_a_stack_takes_the_stack_s_neighbours():
    """
    The panes of a stack sit above one another, so none of them is to
    the left of another. The pane beside the stack runs the whole
    height of it, so it is across from every pane in it.
    """
    left, top, bottom, right = a_pane(), a_pane(), a_pane(), a_pane()
    window = a_window(VSplit([left, HSplit([top, bottom]), right]))

    for pane in (top, bottom):
        assert beside(window, pane, Side.LEFT) is left
        assert beside(window, pane, Side.RIGHT) is right


def test_a_stack_beside_us_is_named_by_the_pane_sharing_most_of_our_edge():
    """
    A whole stack is to our left, and a bar names one pane. The one it
    names is the one our own edge runs along for longest.

    **This is the answer that changed.** The tree said "the top of the
    stack", whatever a person could see beside them; the frame said
    "whichever pane my own row runs into", which nothing could ask
    during a frame. Neither is here now.
    """
    top, bottom, alone = a_pane(), a_pane(), a_pane()
    window = a_window(VSplit([HSplit([top, bottom]), alone]))

    # An even stack in 24 rows gives the top pane the odd row, so it
    # shares one more row with us than the bottom one does.
    assert beside(window, alone, Side.LEFT) is top


def test_the_pane_beside_us_follows_the_edge_we_share():
    "The other half of the same rule, said with a stack that is uneven."
    top, bottom, alone = a_pane(), a_pane(), a_pane()
    window = a_window(VSplit([HSplit([top, bottom]), alone]))
    window.root[0].weights[top] = 1
    window.root[0].weights[bottom] = 10

    assert beside(window, alone, Side.LEFT) is bottom


# ----------------------------------------------------------------------
# A neighbour that is itself a row.


def test_a_row_beside_us_gives_the_pane_that_touches_us():
    """
    The pane to name is the one against our own edge: the rightmost of
    a column on our left, and the leftmost of a column on our right.
    That is the nearest one, and nearest wins before anything else.
    """
    first, second, middle, third, fourth = (a_pane() for _ in range(5))
    window = a_window(
        VSplit([VSplit([first, second]), middle, VSplit([third, fourth])])
    )

    assert beside(window, middle, Side.LEFT) is second
    assert beside(window, middle, Side.RIGHT) is third


# ----------------------------------------------------------------------
# The way a person reaches these shapes.


def test_splitting_a_strip_gives_each_column_its_neighbours():
    "What `split-window -h` builds, three times over."
    window = Window()
    opened = [a_pane()]
    window.add_pane(opened[0])
    window.strip = True

    for _ in range(2):
        opened.append(a_pane())
        window.add_pane(opened[-1], vsplit=True)

    assert beside(window, opened[1], Side.LEFT, Strip) is opened[0]
    assert beside(window, opened[1], Side.RIGHT, Strip) is opened[2]
    assert beside(window, opened[2], Side.RIGHT, Strip) is None
