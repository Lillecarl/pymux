"""
The tint that says a pane runs off the edge. Lillecarl/pymux#222.

A strip is a row that may be wider than the view, so a column can be
cut, and Lillecarl/pymux#218 settled which end is shown: the left one,
because that is where applications write. **That leaves the other half
of the question.** The cells a person sees are the ordinary cells of
that pane, so a pane whose program wrote nothing past the cut looks
exactly like a pane that ends there.

Carl: "what would be nice would be if the rightmost visible column had
some slightly tinted background to indicate that it's cut-off."

**The tint goes into the style of the pane**, not onto the cells after
they are written. Two reasons, and the first one is not a choice:

- **The cells are written later than any place that could mark them.**
  `PlanContainer` asks each pane's container to draw, and those defer
  through `Screen.draw_all_floats`, one function at a time, each free
  to add more. So a pass that appends a class to the cells the panes
  drew runs *before* they are there. This was measured, not guessed.
- A class in a pane's own style is one string per pane per frame.
  Marking the cells by hand came to 14,620 bytecode instructions a
  frame, 43% on top of the frame it marked.

The cost is that a cell the program coloured itself keeps its colour:
a parent's class is less precise than a cell's own `bg:`. So the tint
shows wherever the program left the background alone, which in a shell
is all of it.

This judges the frame a real session draws, because the question is
what a person sees.
"""

import io
from contextlib import contextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from session import Connection
from pymux.layout import CUT_IS_TINTED
from pymux.main import Pymux
from pymux.plan_container import PlanContainer

ROWS = 12
COLUMNS = 80

STRIP = ["set-option pane-border-status on", "set-window-option strip on"]


@contextmanager
def create_client(commands=(), rows=ROWS, columns=COLUMNS):
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=rows, columns=columns)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=Connection(),
        )
        try:
            with set_app(state.app):
                for command in commands:
                    pymux.handle_command(command)

                def draw():
                    screen = Screen()
                    state.app.layout.container.write_to_screen(
                        screen,
                        MouseHandlers(),
                        WritePosition(xpos=0, ypos=0, width=columns, height=rows),
                        "",
                        False,
                        None,
                    )
                    screen.draw_all_floats()
                    state.app.renderer._last_screen = screen
                    return screen

                yield pymux, state, draw
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def panes_of(state) -> PlanContainer:
    found = []

    def walk(container):
        if isinstance(container, PlanContainer):
            found.append(container)
        for child in container.get_children():
            walk(child)

    walk(state.app.layout.container)
    assert len(found) == 1, found
    return found[0]


def marked(screen, row: int) -> str:
    """
    One row of the screen, as the cells that carry the tint.

    A hash for a cell that is marked cut and a space for one that is
    not, so a row reads as a picture.
    """
    return "".join(
        "#" if CUT_IS_TINTED in screen.data_buffer[row][x].style else " "
        for x in range(COLUMNS)
    )


def create_wide_column(pymux, state):
    """
    Two columns, the second one two thirds of the window, with the
    focus back on the first.

    Two halves fit exactly, which is why a half is the default, so a
    column has to be widened for anything to be cut at all. The focus
    goes back to the first column because the view follows the focus:
    on the second one the view scrolls and the first is what is cut.
    """
    pymux.handle_command("split-window -h")
    pymux.handle_command("switch-column-width")
    pymux.handle_command("select-pane -L")
    state.sync_focus()


def test_nothing_is_marked_when_every_column_fits():
    "Two columns of half a window each, which is the default."
    with create_client(STRIP) as (pymux, state, draw):
        pymux.handle_command("split-window -h")
        state.sync_focus()
        screen = draw()

        assert marked(screen, ROWS // 2).strip() == ""


def test_the_column_that_runs_off_the_edge_is_marked():
    with create_client(STRIP) as (pymux, state, draw):
        create_wide_column(pymux, state)
        screen = draw()

        panes = panes_of(state)
        left, right = pymux.arrangement.get_active_window().panes
        cut = panes.plan.rect_of(right)

        # The plan says the second column runs past the view.
        assert cut.right > panes.view.size.columns
        assert panes.view.cuts(cut)

        # And every cell of it that is shown carries the tint, while
        # the column a person is working in carries none.
        row = marked(screen, ROWS // 2)
        assert row[: cut.x] == " " * cut.x, row
        assert row[cut.x :] == "#" * (COLUMNS - cut.x), row
        assert panes.plan.rect_of(left).right <= cut.x


def test_the_tint_sits_before_what_the_pane_wrote():
    """
    Which is what makes a program's own colours survive it.

    The class comes from the pane's style, so it is in the string
    before anything the cell itself carries, and a style further to
    the right wins. A cell the program coloured keeps its colour; one
    it left alone takes the tint.
    """
    with create_client(STRIP) as (pymux, state, draw):
        create_wide_column(pymux, state)
        screen = draw()

        cut = panes_of(state).plan.rect_of(
            pymux.arrangement.get_active_window().panes[1]
        )
        style = screen.data_buffer[ROWS // 2][cut.x].style

        assert CUT_IS_TINTED in style
        # The pane's own class is still there, and the tint is beside
        # it rather than on top of it.
        assert "class:terminal" in style
        assert style.index("class:terminal") < style.index(CUT_IS_TINTED)
