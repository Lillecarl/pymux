"""
Hold the cost of laying a window out to a budget.

Every check in this repository asks whether pymux draws the right
cells. None of them asks what drawing them costs, so a change that
makes a frame ten times more expensive passes all of them, and nobody
notices until pymux feels wrong under a hand. Lillecarl/pymux#217
rewrote the layout engine, which is exactly the kind of change that
could do it.

The unit is bytecode instructions and not seconds. `tests/instructions.py`
says why, and what moves a count that is not a change in the code.

## What it measures, and what it does not

**Not the panes.** A pane's own cost is ptterm's -- parsing the bytes
and building the rows -- and `checks.ptterm-instructions` already holds
that to a budget. Here each pane is an empty `Window`, so what is left
in the count is the layout, the chrome and prompt_toolkit's frame
around them.

Four numbers per shape:

- **measure**, one plan of the whole window. This is the arithmetic:
  the tree walk, the shares, one `Slot` and one `Rect` per pane.
- **chrome**, the lines that fill the gaps the plan left.
- **frame**, one `PlanContainer.write_to_screen`: measure, look at,
  paint the chrome, and write every pane at its rectangle.
- **neighbours**, the four questions each title bar asks -- what is to
  my left, right, above and below -- for every pane of the window.
  **This is the number to watch.** A bar is drawn for every pane on
  every frame, so this is paid as often as the frame is, and each
  question goes through `plan_of`.

And one count that is not a cost:

- **plans**, how many times a plan is measured while all of the above
  runs. It is a count of calls, not of instructions, and it says
  directly what "unnecessary repaints" means here: one frame needs one
  plan, and every plan past the first is work done twice.

## The shapes

One pane, four, and sixteen, in each of the two layouts, because the
question is how the cost grows and not what it is at one size. Sixteen
panes is a wall of terminals, and it is the size at which anything
quadratic stops hiding.

A zoomed window of sixteen panes is measured as well. It should cost
what one pane costs, because that is all it lays out.

## What it is judged against

`tests/frame-budgets.txt` holds one line per measurement. A run that
differs from its budget by more than the tolerance fails, in either
direction: a count that climbed is the fault this check is for, and a
count that fell is a budget nobody updated.

    nix build --file . checks.pymux-frame-instructions.run
    less result/log
    cp result/frame-budgets.txt pymux/tests/frame-budgets.txt

Two knobs reach this file from `pymux/nix/checks.nix`:

    PYMUX_FRAME_INCLUDE=strip nix build --file . checks.pymux-frame-instructions
    PYMUX_FRAME_TOLERANCE=2 nix build --file . checks.pymux-frame-instructions
"""

import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# And pymux itself, which the sandbox copies beside `tests` rather than
# installing. A script's own directory is what Python puts on the path,
# not the directory above it.
sys.path.insert(1, str(Path(__file__).parent.parent))

from instructions import count_instructions  # noqa: E402
from prompt_toolkit.application import Application  # noqa: E402
from prompt_toolkit.application.current import set_app  # noqa: E402
from prompt_toolkit.data_structures import Size  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.layout.containers import Window  # noqa: E402
from prompt_toolkit.layout.layout import Layout  # noqa: E402
from prompt_toolkit.layout.mouse_handlers import MouseHandlers  # noqa: E402
from prompt_toolkit.layout.screen import Screen, WritePosition  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from pymux.arrangement import Pane, Window as ArrangementWindow  # noqa: E402
from pymux.divided import Divided  # noqa: E402
from pymux.layout import (  # noqa: E402
    layout_of,
    pane_beside,
    room_for_the_panes,
)
from pymux.plan_container import PlanContainer  # noqa: E402
from pymux.plane import Side  # noqa: E402
from pymux.strip import Strip  # noqa: E402
from pymux.zoomed import Zoomed  # noqa: E402

HERE = Path(__file__).parent

#: The counts that this check expects, one measurement per line.
BUDGETS = HERE / "frame-budgets.txt"

#: How far a count may move from its budget before the check fails, as
#: a percentage. A refactor moves a count a little; the fault this
#: check is for moves it by a lot.
DEFAULT_TOLERANCE = 5.0

#: The client this measures a frame for. It is a terminal nobody
#: resized, less the row the status line takes.
ROWS = 23
COLUMNS = 80

#: How many panes each shape holds.
COUNTS = (1, 4, 16)


class _Terminal:
    "Enough of a terminal for the arrangement to hold a pane."


class _Manager:
    """
    What `plan_of` asks a client's layout manager, and no more.

    The real one is `layout.LayoutManager`, and these three methods are
    the same three: the container that drew the frame, and the plan of
    the frame kept for everything else drawn in it. Keeping it here is
    what makes the neighbour measurement a frame rather than a
    question: the first question works the plan out and the rest read
    it, which is what a real title bar does.
    """

    def __init__(self, container) -> None:
        self._container = container
        self._plan_of_the_frame = None

    def pane_container(self):
        return self._container

    def plan_of_this_frame(self, window, size):
        if self._plan_of_the_frame is None:
            return None

        drawn_for, drawn_at, plan = self._plan_of_the_frame
        if drawn_for is not window or drawn_at != size:
            return None

        return plan

    def remember_the_plan(self, window, size, plan) -> None:
        self._plan_of_the_frame = (window, size, plan)


class _ClientState:
    "A client that has drawn a frame, as far as the layout can tell."

    def __init__(self, container) -> None:
        self.layout_manager = _Manager(container)


class _Pymux:
    """
    What the layout asks a server for, and nothing else.

    A real `Pymux` starts a socket and a process per pane, and none of
    that is in the question. What the layout reads is the size of the
    window, whether the pane status is on, and the client that is
    drawing.

    `state` is what makes the two neighbour measurements different.
    With one, a title bar reads the plan of the frame it is being drawn
    in; without one, there is no frame yet and the question is measured
    fresh, which is what a key press does before anything is drawn.
    """

    show_pane_status = True

    def __init__(self) -> None:
        self.state = None

    def the_size_of_the_plane(self, window=None) -> Size:
        return Size(rows=ROWS, columns=COLUMNS)

    def get_client_state(self):
        if self.state is None:
            # What a real server says when nothing is attached, and
            # what `plan_of` catches.
            raise ValueError("no client is attached")
        return self.state


def a_window(count: int, strip: bool = False):
    """
    A window of `count` panes, and the panes in it.

    The splits alternate, so the tree is the shape a person builds by
    splitting the pane they are looking at: a row, then a stack inside
    it, then a row inside that. A strip puts every other pane in a
    column of its own and stacks the rest, which is what a niri column
    holds.
    """
    window = ArrangementWindow()
    panes = [Pane(terminal=_Terminal())]
    window.add_pane(panes[0])
    window.strip = strip

    for number in range(1, count):
        panes.append(Pane(terminal=_Terminal()))
        window.add_pane(panes[-1], vsplit=number % 2 == 1)

    return window, panes


def a_container(pymux, window, panes):
    "The container that draws this window, with an empty pane in each."
    return PlanContainer(
        layout_of(pymux, window), {pane: Window() for pane in panes}
    )


@contextmanager
def an_application(container):
    """
    A current application, because a frame asks which pane has the
    keyboard and that is a fact about an application.
    """
    with create_pipe_input() as pipe:
        app = Application(layout=Layout(container), input=pipe, output=DummyOutput())
        with set_app(app):
            yield app


def a_frame(container, room: Size):
    """
    One whole frame of the panes of a window, onto a fresh screen.

    `room` is what the chrome leaves, because that is the write
    position the real layout gives this container: the rows for the
    bars above and below the panes come off before it is asked to
    draw. A frame drawn at any other size would measure a plan that no
    title bar could then read.
    """

    def work():
        container.write_to_screen(
            Screen(),
            MouseHandlers(),
            WritePosition(xpos=0, ypos=0, width=room.columns, height=room.rows),
            "",
            False,
            None,
        )

    return work


def the_neighbours(pymux, window, panes):
    "What every title bar of this window asks, once each."

    def work():
        for pane in panes:
            for side in Side:
                pane_beside(pymux, window, pane, side)

    return work


@contextmanager
def counted_plans():
    """
    Count how many plans are measured inside this block.

    Every layout is patched, because the question is how many plans a
    frame builds and not which class built them. `pane_beside`
    builds one of its own for each question it answers, so this is
    where work done twice shows up.
    """
    counts = {"plans": 0}
    originals = {}

    def counting(original):
        def measure(self, available):
            counts["plans"] += 1
            return original(self, available)

        return measure

    for layout in (Divided, Strip, Zoomed):
        originals[layout] = layout.measure
        layout.measure = counting(layout.measure)

    try:
        yield counts
    finally:
        for layout, original in originals.items():
            layout.measure = original


def measurements(include: str):
    """
    Every measurement this run takes, as (name, work) pairs.

    The work is a callable, so building the window and the container
    stays outside the count.
    """
    pymux = _Pymux()
    found = []

    def take(name: str, work, state=None) -> None:
        """
        One measurement, and which client is attached while it runs.

        The client decides whether a neighbour question reads the plan
        of a frame or measures one, so it is part of what is measured
        and not part of the setup.
        """
        if not include or re.search(include, name):
            found.append((name, work, state))

    for count in COUNTS:
        for what, strip in (("divided", False), ("strip", True)):
            shape = "%s %d panes" % (what, count)
            window, panes = a_window(count, strip)
            room = room_for_the_panes(pymux, window)
            layout = layout_of(pymux, window)
            plan = layout.measure(room)

            container = a_container(pymux, window, panes)
            with an_application(container):
                a_frame(container, room)()

            take("%s (measure)" % shape, lambda la=layout, r=room: la.measure(r))
            take("%s (chrome)" % shape, lambda la=layout, p=plan: la.chrome(p))
            take("%s (frame)" % shape, a_frame(container, room))
            take(
                "%s (neighbours)" % shape,
                the_neighbours(pymux, window, panes),
                _ClientState(container),
            )
            take("%s (neighbours cold)" % shape, the_neighbours(pymux, window, panes))

    # A zoomed window lays out one pane, whatever it holds. This is the
    # only measurement of a wrapper, and it is here to say that the
    # wrapper costs nothing.
    window, panes = a_window(max(COUNTS))
    window.zoom = True
    room = room_for_the_panes(pymux, window)
    take(
        "zoomed %d panes (frame)" % max(COUNTS),
        a_frame(a_container(pymux, window, panes), room),
    )

    return found, pymux


def plans_of_a_frame(pymux, include: str):
    """
    How many plans one frame of each shape measures.

    A frame needs one: the container measures, and everything drawn
    inside it reads what the container measured. Every plan past that
    is the same arithmetic over the same window, inside the same
    frame.
    """
    found = []

    for count in COUNTS:
        for what, strip in (("divided", False), ("strip", True)):
            name = "%s %d panes (plans)" % (what, count)
            if include and not re.search(include, name):
                continue

            window, panes = a_window(count, strip)
            container = a_container(pymux, window, panes)
            room = room_for_the_panes(pymux, window)
            pymux.state = _ClientState(container)

            try:
                with an_application(container):
                    with counted_plans() as counts:
                        a_frame(container, room)()
                        the_neighbours(pymux, window, panes)()
            finally:
                pymux.state = None

            found.append((name, counts["plans"]))

    return found


def read_budgets(path: Path):
    "The recorded count of each measurement."
    budgets = {}
    if not path.is_file():
        return budgets
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, count = line.rsplit(None, 1)
        budgets[name] = int(count)
    return budgets


HEADER = """\
# What it costs pymux to lay a window out and draw the frame around its
# panes, in bytecode instructions. `tests/measure_a_frame.py` says why
# the unit is not a second, and what each measurement covers.
#
# The "(plans)" lines are not instructions. They count how many plans
# one frame measures, and a frame needs one.
#
# This is what the run saw. To make it what the check expects:
#     nix build --file . checks.pymux-frame-instructions.run
#     cp result/frame-budgets.txt pymux/tests/frame-budgets.txt
"""


def main() -> int:
    include = os.environ.get("PYMUX_FRAME_INCLUDE", "")
    tolerance = float(os.environ.get("PYMUX_FRAME_TOLERANCE", "") or DEFAULT_TOLERANCE)

    found, pymux = measurements(include)
    plans = plans_of_a_frame(pymux, include)

    if not found and not plans:
        print("Nothing matched %r, so this run measured nothing." % include)
        return 1

    counts = {}
    wrong = []

    def judge(name: str, counted: int) -> None:
        counts[name] = counted
        budget = budgets.get(name)
        if budget is None:
            print("%-34s %12d  (no budget yet)" % (name, counted))
            wrong.append(name)
            return

        moved = 100.0 * (counted - budget) / budget
        mark = "ok " if abs(moved) <= tolerance else "OFF"
        print(
            "%-34s %12d  budget %12d  %+6.2f%%  %s"
            % (name, counted, budget, moved, mark)
        )
        if abs(moved) > tolerance:
            wrong.append(name)

    budgets = read_budgets(BUDGETS)

    for name, work, state in found:
        pymux.state = state
        try:
            with an_application(a_container(pymux, *a_window(1))):
                judge(name, count_instructions(work))
        finally:
            pymux.state = None

    print()
    for name, counted in plans:
        judge(name, counted)

    out = os.environ.get("PYMUX_FRAME_OUT", "")
    if out:
        report = HEADER + "".join(
            "%-34s %d\n" % (name, counts[name]) for name in sorted(counts)
        )
        (Path(out) / "frame-budgets.txt").write_text(report)

    if include:
        print(
            "\nThis run measured %d of the shapes, so it makes no claim "
            "about the rest." % (len(found) + len(plans))
        )

    if wrong:
        print(
            "\n%d of %d moved by more than %.1f%%: %s"
            % (len(wrong), len(counts), tolerance, ", ".join(sorted(wrong)))
        )
        print(
            "A count that climbed is what this check is for. A count that "
            "fell is a budget nobody updated. Read the numbers, then:"
        )
        print("    cp result/frame-budgets.txt pymux/tests/frame-budgets.txt")
        return 1

    print("\nEvery measurement is within %.1f%% of its budget." % tolerance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
