"""
Drawing a plan, and moving the view over it.

`PlanContainer` puts each pane where the plan says, less the offset of
the view. It is `ScrollableStrip` for two axes instead of one, and it
is what makes the plan the truth rather than a second opinion about a
frame somebody else draws. Lillecarl/pymux#217.

**The tests read the cells.** Every way this can be wrong -- an
off-by-one in the offset, a pane drawn at the wrong place, a view that
does not follow the focus -- shows as the wrong letters in a row.
Asking the container what it thinks its offset is would miss all of
them. `test_strip.py` judges the older container the same way.

Each pane is filled with a letter of its own, so a row of the screen
says which part of the plane is on it.
"""

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput

from pymux.plan_container import PlanContainer
from pymux.plane import Line, Plan, Rect, Slot

HEIGHT = 2

#: One letter for each pane, in the order they go on the plane.
LETTERS = "abcdefgh"


class _Pane:
    "A pane is anything with an identity, as far as a plan is concerned."

    def __init__(self, letter: str) -> None:
        self.letter = letter

    def __repr__(self) -> str:
        return self.letter


class _Fixed:
    """
    A layout that always says the same thing.

    `PlanContainer` asks a layout two questions and this answers both
    from what it was given, so a test can put a rectangle anywhere,
    including off the screen and behind the origin.
    """

    def __init__(self, plan: Plan, offset: Point = Point(x=0, y=0)) -> None:
        self.plan = plan
        self.offset = offset

    def measure(self, available: Size) -> Plan:
        return self.plan

    def look_at(self, plan, view, focus) -> Point:
        return self.offset


def create_pane(letter: str, width: int, height: int = HEIGHT):
    "A pane, and a window that fills itself with one letter."
    return _Pane(letter), Window(
        content=FormattedTextControl([("", "\n".join([letter * width] * height))]),
    )


def a_row(widths, height=HEIGHT, gap=0):
    """
    A plan of panes side by side, and their containers.

    `gap` is the cells left between them, which is where a border
    goes. Nothing draws in the gap here: this judges where the panes
    land.
    """
    rects = {}
    containers = {}
    x = 0

    for letter, width in zip(LETTERS, widths):
        pane, container = create_pane(letter, width, height)
        rects[Slot(pane)] = Rect(x=x, y=0, width=width, height=height)
        containers[pane] = container
        x += width + gap

    return Plan(rects), containers


def drawn(plan, containers, visible, offset=Point(x=0, y=0), rows=HEIGHT, row=0):
    """
    One row of the screen, as a string.

    A real application has to be current, because a container asks
    which window has the focus, and that is a fact about an
    application.
    """
    container = PlanContainer(_Fixed(plan, offset), containers)

    with create_pipe_input() as pipe:
        app = Application(layout=Layout(container), input=pipe, output=DummyOutput())
        with set_app(app):
            screen = Screen()
            container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=visible, height=rows),
                "",
                False,
                None,
            )

    return "".join(screen.data_buffer[row][x].char for x in range(visible))


# ----------------------------------------------------------------------
# Where the panes land.


def test_every_pane_is_drawn_where_the_plan_puts_it():
    plan, containers = a_row([4, 4])

    assert drawn(plan, containers, visible=8) == "aaaabbbb"


def test_a_gap_in_the_plan_is_left_alone():
    "It is where a border goes, and a border is not a pane."
    plan, containers = a_row([4, 4], gap=1)

    assert drawn(plan, containers, visible=9) == "aaaa bbbb"


def test_a_plan_wider_than_the_view_shows_what_fits():
    plan, containers = a_row([4, 4, 4])

    assert drawn(plan, containers, visible=8) == "aaaabbbb"


def test_the_offset_says_which_part_is_on_screen():
    plan, containers = a_row([4, 4, 4])

    assert drawn(plan, containers, visible=8, offset=Point(x=4, y=0)) == "bbbbcccc"


def test_an_offset_between_two_panes_shows_both():
    "Nothing says a view may only stop on an edge."
    plan, containers = a_row([4, 4, 4])

    assert drawn(plan, containers, visible=8, offset=Point(x=2, y=0)) == "aabbbbcc"


def test_a_plan_narrower_than_the_view_leaves_the_rest_alone():
    plan, containers = a_row([4])

    assert drawn(plan, containers, visible=8) == "aaaa    "


def test_a_pane_behind_the_origin_is_clipped_and_not_lost():
    """
    The trick the whole thing rests on. A pane scrolled off the left
    is written at a negative position, and the renderer reads only the
    rectangle it shows, so the cells outside cost nothing.
    """
    plan, containers = a_row([4, 4])

    assert drawn(plan, containers, visible=4, offset=Point(x=6, y=0)) == "bb  "


def test_the_view_moves_down_as_well_as_sideways():
    "Which is what this container has that `ScrollableStrip` does not."
    pane, container = create_pane("a", 4, height=4)
    plan = Plan({Slot(pane): Rect(x=0, y=2, width=4, height=4)})

    on_the_plane = drawn(plan, {pane: container}, visible=4, rows=4, row=2)
    moved = drawn(
        plan, {pane: container}, visible=4, rows=4, offset=Point(x=0, y=2), row=0
    )

    assert on_the_plane == "aaaa"
    assert moved == "aaaa"


# ----------------------------------------------------------------------
# The lines the layout puts in its gaps.


class _WithChrome(_Fixed):
    "A layout that draws something in the gap it left."

    def __init__(self, plan, lines, offset=Point(x=0, y=0)):
        super().__init__(plan, offset)
        self.lines = lines

    def chrome(self, plan):
        return self.lines


def drawn_with_chrome(plan, containers, lines, visible, rows=HEIGHT, row=0):
    container = PlanContainer(_WithChrome(plan, lines), containers)

    with create_pipe_input() as pipe:
        app = Application(layout=Layout(container), input=pipe, output=DummyOutput())
        with set_app(app):
            screen = Screen()
            container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=visible, height=rows),
                "",
                False,
                None,
            )

    return "".join(screen.data_buffer[row][x].char for x in range(visible))


def test_the_layout_fills_the_gap_it_left():
    """
    A pane knows nothing about borders. Carl: "individual panes should
    not be aware of borders ... the layout is responsible for drawing
    the borders either way."
    """
    plan, containers = a_row([4, 4], gap=1)
    line = Line(Rect(x=4, y=0, width=1, height=HEIGHT), "|")

    assert drawn_with_chrome(plan, containers, [line], visible=9) == "aaaa|bbbb"


def test_a_pane_is_drawn_over_a_line():
    "The panes go on last, so a line under one is not seen."
    plan, containers = a_row([4, 4])
    line = Line(Rect(x=0, y=0, width=8, height=HEIGHT), "|")

    assert drawn_with_chrome(plan, containers, [line], visible=8) == "aaaabbbb"


def test_a_line_outside_the_view_is_not_drawn_on_it():
    plan, containers = a_row([4], gap=1)
    line = Line(Rect(x=4, y=0, width=1, height=HEIGHT), "|")

    assert drawn_with_chrome(plan, containers, [line], visible=4) == "aaaa"


def test_a_layout_with_no_chrome_draws_none():
    "A layout need not have lines. `_Fixed` has no `chrome` at all."
    plan, containers = a_row([4, 4], gap=1)

    assert drawn(plan, containers, visible=9) == "aaaa bbbb"


# ----------------------------------------------------------------------
# A stack draws the pane a person sees.


def test_a_slot_draws_the_pane_it_shows_and_no_other():
    behind, its_container = create_pane("a", 4)
    front, other = create_pane("b", 4)
    slot = Slot(behind, front)
    plan = Plan({slot: Rect(x=0, y=0, width=4, height=HEIGHT)})
    containers = {behind: its_container, front: other}

    assert drawn(plan, containers, visible=4) == "aaaa"

    slot.show(front)
    assert drawn(plan, containers, visible=4) == "bbbb"


# ----------------------------------------------------------------------
# What the container tells the rest of the frame.


def test_the_container_holds_the_plan_it_drew():
    """
    A title bar is drawn inside one of these panes, during the frame,
    so it has to be able to ask what the frame is. That is the two
    answers problem going away.
    """
    plan, containers = a_row([4, 4])
    container = PlanContainer(_Fixed(plan), containers)

    assert container.plan is None

    with create_pipe_input() as pipe:
        app = Application(layout=Layout(container), input=pipe, output=DummyOutput())
        with set_app(app):
            container.write_to_screen(
                screen := Screen(),
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=8, height=HEIGHT),
                "",
                False,
                None,
            )

    assert container.plan is plan
    assert screen.data_buffer[0][0].char == "a"


def test_the_container_names_the_pane_that_has_the_keyboard():
    plan, containers = a_row([4, 4])
    container = PlanContainer(_Fixed(plan), containers)
    wanted = list(containers)[1]

    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(container, focused_element=containers[wanted]),
            input=pipe,
            output=DummyOutput(),
        )
        with set_app(app):
            assert container.focused_pane() is wanted


def test_a_pane_with_no_container_is_not_drawn():
    "And does not stop the frame: a bar is drawn on every one."
    plan, containers = a_row([4, 4])
    containers.pop(list(containers)[0])

    assert drawn(plan, containers, visible=8) == "    bbbb"


def _drawn_at(plan, containers, offset, visible=8):
    "Where each pane's container was drawn, on the screen."
    container = PlanContainer(_Fixed(plan, offset), containers)

    with create_pipe_input() as pipe:
        app = Application(layout=Layout(container), input=pipe, output=DummyOutput())
        with set_app(app):
            screen = Screen()
            container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(xpos=0, ypos=0, width=visible, height=HEIGHT),
                "",
                False,
                None,
            )

    return screen.visible_windows_to_write_positions


def test_a_pane_is_told_where_it_ended_up():
    "A click has to reach the pane that was drawn under it."
    plan, containers = a_row([4, 4, 4])
    panes = list(containers)

    where = _drawn_at(plan, containers, Point(x=2, y=0))

    # The view is two cells along, so the first pane was drawn two
    # cells to the left of the screen, and half of it is on.
    assert where[containers[panes[0]]].xpos == -2
    assert where[containers[panes[1]]].xpos == 2


def test_a_pane_with_no_part_of_it_in_the_view_is_not_drawn():
    """
    Building the rows of a pane nobody can see costs more than the
    cells it throws away, so it is not written at all.
    Lillecarl/pymux#224.

    A pane that is *partly* in the view is written whole and clipped,
    which is what the plane rests on, and the test above holds that.
    """
    plan, containers = a_row([4, 4, 4])
    panes = list(containers)

    where = _drawn_at(plan, containers, Point(x=4, y=0))

    assert containers[panes[0]] not in where
    assert where[containers[panes[1]]].xpos == 0


def test_the_container_asks_for_no_size_of_its_own():
    """
    The view scrolls, so any size will do. Asking for the size the plan
    wants is what every other layout does, and what this exists to
    avoid.
    """
    plan, containers = a_row([4, 4, 4])
    container = PlanContainer(_Fixed(plan), containers)

    assert container.preferred_width(80).preferred <= 80
    assert container.preferred_height(80, 24).preferred <= 24


def test_a_row_is_as_wide_as_the_screen_whatever_the_plan_is():
    plan, containers = a_row([4, 4, 4])

    for visible in (1, 3, 7, 12, 40):
        assert len(drawn(plan, containers, visible=visible)) == visible
