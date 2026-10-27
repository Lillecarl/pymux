"""
The shade that says a pane runs off the edge. Lillecarl/pymux#222,
Lillecarl/pymux#616.

A strip is a row that may be wider than the view, so a column can be
cut, and Lillecarl/pymux#218 settled which end is shown. **That leaves
the other half of the question.** The cells a person sees are the
ordinary cells of that pane, so a pane whose program wrote nothing past
the cut looks exactly like a pane that ends there.

Carl: "the tint i asked for was not full-screen, just the column near
the edge. If it's not too compute heavy we could do a 'few' column
gradient even." And of the whole-pane tint before it: "the inconsistent
application of it" -- it reached only the cells a program left at the
default background, and only the pane, never the side.

So the cells nearest the cut edge are shaded, strongest at the edge and
fading inward over `cut-shade-width` cells, on whichever side is cut, and
each cell's own background is what moves.

This judges the frame a real session draws, because the question is
what a person sees.
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from pyte.colors import parse_color
from run_commands import run
from session import Connection

from pymux.cut_shade import hex_of
from pymux.main import Pymux
from pymux.plan_container import PlanContainer
from pymux.style import tinted

ROWS = 12
COLUMNS = 80
ROW = ROWS // 2

STRIP = ["set-option pane-border-status on", "set-window-option strip on"]

#: `conftest.py` paints the screen for every server this suite builds.
#: With it off a default cell shows the terminal's own background.
PAINT_SCREEN_OFF = "set-option paint-screen off"


@asynccontextmanager
async def create_client(commands=(), rows=ROWS, columns=COLUMNS):
    pymux = Pymux()
    output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: Size(rows=rows, columns=columns))
    async with pymux.running():
        with create_pipe_input() as pipe:
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=Connection(),
            )
            try:
                with set_app(state.app):
                    # What attaching brings: the bindings, and the
                    # window the drawing below looks at.
                    await pymux.startup()
                    for command in commands:
                        await run(pymux, command)

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


def backgrounds(state, screen, row: int = ROW) -> list[str | None]:
    "The background each cell of a row really shows, as `#rrggbb`."
    resolve = state.app.renderer.style.get_attrs_for_style_str
    return [hex_of(resolve(screen.data_buffer[row][x].style).bgcolor or "") for x in range(COLUMNS)]


async def create_wide_column(pymux, state, focus_left=True):
    """
    Two columns, the second one two thirds of the window.

    Two halves fit exactly, which is why a half is the default, so a
    column has to be widened for anything to be cut at all. With the
    focus on the first column the second runs off the right; with it on
    the second, the view scrolls and the first is cut on the left.
    """
    await run(pymux, "split-window -h")
    await run(pymux, "switch-column-width")
    if focus_left:
        await run(pymux, "select-pane -L")
    state.sync_focus()


def a_fade(pymux, plain: str) -> list[str]:
    "What a band of the shade over `plain` should be, from the cut edge inward."
    width, strength = pymux.session_defaults.cut_shade_width, pymux.session_defaults.cut_shade_strength / 100
    return [tinted(plain, strength * (width - inward) / width) for inward in range(width)]


def at_the_edge(pymux, plain: str) -> str:
    return tinted(plain, pymux.session_defaults.cut_shade_strength / 100)


async def test_nothing_is_shaded_when_every_column_fits():
    "Two columns of half a window each, which is the default."
    async with create_client(STRIP) as (pymux, state, draw):
        await run(pymux, "split-window -h")
        state.sync_focus()
        shown = backgrounds(state, draw())

        assert len({colour for colour in shown if colour is not None}) == 1, shown


async def test_a_column_cut_on_the_right_fades_in_at_the_right_edge():
    async with create_client(STRIP) as (pymux, state, draw):
        await create_wide_column(pymux, state)
        screen = draw()
        panes = panes_of(state)
        cut = panes.plan.rect_of(pymux.arrangement.get_active_window().panes[1])
        assert cut.right > COLUMNS

        shown = backgrounds(state, screen)
        plain = shown[cut.x]
        width = pymux.session_defaults.cut_shade_width
        # Only the last few cells, strongest at the edge.
        assert shown[cut.x : COLUMNS - width] == [plain] * (COLUMNS - width - cut.x)
        assert shown[COLUMNS - width :][::-1] == a_fade(pymux, plain)


async def test_a_column_cut_on_the_left_fades_in_at_the_left_edge():
    "The same mark on the other side: the one place the whole-pane tint never was."
    async with create_client(STRIP) as (pymux, state, draw):
        await create_wide_column(pymux, state, focus_left=False)
        screen = draw()
        panes = panes_of(state)
        left = panes.plan.rect_of(pymux.arrangement.get_active_window().panes[0])
        assert left.x < panes.view.shown_rect.x

        shown = backgrounds(state, screen)
        width = pymux.session_defaults.cut_shade_width
        assert shown[:width] == a_fade(pymux, shown[width])


async def test_a_cell_the_program_coloured_fades_too():
    """
    The inconsistency Carl saw: a class in the pane's style reached only
    the cells a program left alone. The shade moves each cell's own.
    """
    async with create_client(STRIP) as (pymux, state, draw):
        await create_wide_column(pymux, state)
        right = pymux.arrangement.get_active_window().panes[1]
        red = ("\x1b[48;2;170;0;0m" + " " * 200 + "\r\n") * ROWS
        right.terminal.terminal_control.feed_output("\x1b[2J\x1b[H" + red)
        screen = draw()

        assert backgrounds(state, screen)[COLUMNS - 1] == at_the_edge(pymux, "#aa0000")


async def test_the_shade_moves_the_terminals_own_background():
    """
    With `paint-screen` off, a default cell shows the terminal's own
    background, so the shade starts from what the terminal answered.
    Lillecarl/pymux#352.
    """
    async with create_client([*STRIP, PAINT_SCREEN_OFF]) as (pymux, state, draw):
        state.connection.default_colors.background = parse_color("#404040")
        await create_wide_column(pymux, state)

        assert backgrounds(state, draw())[COLUMNS - 1] == at_the_edge(pymux, "#404040")


async def test_the_width_and_the_strength_are_options():
    "Carl: a bit stronger, and both configurable."
    commands = [*STRIP, "set-option cut-shade-width 2", "set-option cut-shade-strength 30"]
    async with create_client(commands) as (pymux, state, draw):
        await create_wide_column(pymux, state)
        shown = backgrounds(state, draw())
        plain = shown[COLUMNS - 3]

        assert shown[COLUMNS - 2 :] == [tinted(plain, 0.15), tinted(plain, 0.30)]


async def test_a_width_of_nothing_turns_the_shade_off():
    async with create_client([*STRIP, "set-option cut-shade-width 0"]) as (pymux, state, draw):
        await create_wide_column(pymux, state)
        shown = backgrounds(state, draw())

        assert shown[COLUMNS - 1] == shown[COLUMNS - 5]


def _lightness(colour: str) -> int:
    return sum(int(colour[at : at + 2], 16) for at in (1, 3, 5))


async def test_a_dark_terminal_is_lifted():
    assert _lightness(tinted("#000000", 0.1)) > _lightness("#000000")


async def test_a_light_terminal_is_darkened():
    "The same step, the other way. A fixed colour can only do one of these."
    assert _lightness(tinted("#ffffff", 0.1)) < _lightness("#ffffff")
