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

**The colour is the terminal's, not the theme's.** A cell the program
left alone shows the background of the terminal the client runs in, so
a tint picked against the theme's background is a guess: it lifts one
terminal and darkens the next by the same amount. `cut_tint` derives
it from what the client's terminal answered, and the theme's own rule
draws only where the theme owns the background. Lillecarl/pymux#352.

This judges the frame a real session draws, because the question is
what a person sees.
"""

import io
from contextlib import contextmanager

import pytest

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from session import Connection
from pymux.layout import CUT_IS_TINTED, cut_tint
from pymux.main import Pymux
from pymux.plan_container import PlanContainer
from pymux.style import roles_of, roles_of_palette, tinted
from pyte.colors import parse_color

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


def test_column_that_runs_off_edge_is_marked():
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


def test_tint_sits_before_what_pane_wrote():
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


# ----------------------------------------------------------------------
# The colour of it. Lillecarl/pymux#352.


#: `conftest.py` paints the screen for every server this suite builds,
#: and the derived tint is for the case where the theme does *not* own
#: the background -- which is what pymux starts on.
PAINT_SCREEN_OFF = "set-option paint-screen off"


def _says(state, background: str) -> None:
    "A client whose terminal answered `OSC 11` with this colour."
    state.connection.default_colors.background = parse_color(background)


def _lightness(colour: str) -> int:
    return sum(int(colour[at : at + 2], 16) for at in (1, 3, 5))


def test_a_dark_terminal_is_lifted():
    assert _lightness(tinted("#000000")) > _lightness("#000000")


def test_a_light_terminal_is_darkened():
    "The same step, the other way. A fixed colour can only do one of these."
    assert _lightness(tinted("#ffffff")) < _lightness("#ffffff")


@pytest.mark.parametrize(
    "name", ["default", "grey", "base16:gruvbox-dark-hard", "pygments:monokai"]
)
def test_every_source_moves_its_own_background(name):
    """
    One number, so a tint is the same mark in every scheme.

    Each source used to pick its own step, and two of the five picked
    a different one. A source that drifts back to a blend of its own
    is what this catches. Lillecarl/pymux#352.
    """
    roles = roles_of(name)

    assert roles["cut"] == tinted(roles["pane"])


def test_a_palette_moves_its_own_background_too():
    "The fifth source: a theme built from sixteen colours a terminal named."
    roles = roles_of_palette(["#101018"] + ["#808080"] * 15)

    assert roles["cut"] == tinted(roles["pane"])


def test_a_mid_grey_terminal_is_lifted_and_not_holed():
    """
    The case that says a fixed colour is wrong, and it needs no light
    terminal: a screen on `#404040` under the `default` theme, whose
    `cut` role is darker than that. The theme's colour is a hole in
    the screen; the derived one is a lift.
    """
    assert _lightness(roles_of("default")["cut"]) < _lightness("#404040")
    assert _lightness(tinted("#404040")) > _lightness("#404040")


def test_the_column_wears_the_colour_of_the_terminal():
    with create_client(STRIP + [PAINT_SCREEN_OFF]) as (pymux, state, draw):
        _says(state, "#404040")
        create_wide_column(pymux, state)
        screen = draw()

        cut = panes_of(state).plan.rect_of(
            pymux.arrangement.get_active_window().panes[1]
        )
        style = screen.data_buffer[ROWS // 2][cut.x].style

        assert "bg:%s" % (tinted("#404040"),) in style
        # And the theme's own rule is not also there: two rules for one
        # background is one of them winning by accident.
        assert CUT_IS_TINTED not in style


def test_a_terminal_that_answered_nothing_keeps_the_theme_rule():
    "Which is every client with no connection, and every dumb terminal."
    with create_client(STRIP + [PAINT_SCREEN_OFF]) as (pymux, state, draw):
        create_wide_column(pymux, state)

        assert state.connection.default_colors.background is None
        assert cut_tint(pymux) == CUT_IS_TINTED


def test_the_theme_owns_the_background_when_it_paints_the_screen():
    """
    `paint-screen` puts the theme's own colour behind every cell, so
    the theme's `cut` role is picked against the background it is
    really drawn over. Lillecarl/pymux#273.
    """
    with create_client(STRIP + [PAINT_SCREEN_OFF]) as (pymux, state, draw):
        _says(state, "#404040")
        create_wide_column(pymux, state)
        assert cut_tint(pymux) != CUT_IS_TINTED

        pymux.handle_command("set-option paint-screen on")

        assert cut_tint(pymux) == CUT_IS_TINTED
