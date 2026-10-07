"""
Zoom: one pane fills the window, and the layout under it waits.

`resize-pane -Z` used to be a flag the layout tested before anything
else, so a zoomed window was not laid out at all -- the panes beside it
stopped existing for that frame, and a zoomed strip lost its row, its
scrolling and its column widths until a person unzoomed.
Lillecarl/pymux#215.

`Zoomed` is a layout that wraps the one the window has and shows one
pane of it. What it wraps is untouched, which is what tmux does as
well: `window_zoom` saves the tree and `window_unzoom` puts it back.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from test_strip_draws import CHROME, create_client, dump

from pymux.commands import handle_command
from pymux.divided import Divided
from pymux.layout import layout_of, pane_beside, plan_of
from pymux.plane import Side
from pymux.strip import Strip
from pymux.zoomed import Zoomed

ROWS, COLUMNS = 12, 40

STRIP = [*CHROME, "set-window-option strip on"]


async def two_panes(pymux, command="split-window -h"):
    "Two panes in the active window, and the second has the focus."
    window = pymux.arrangement.get_active_window()
    first = window.active_pane
    await run(pymux, command)
    return window, first, window.active_pane


async def run(pymux, command):
    """
    Run a command, and wait for what it started.

    A key binding does not wait, but a test that did not would draw
    before the pane exists. The end state is the same either way.
    """
    answer = handle_command(pymux, command)
    if answer is not None:
        await answer


async def test_zoom_wraps_layout_and_keeps_it():
    async with create_client(CHROME) as (pymux, draw):
        window, _first, _second = await two_panes(pymux)

        assert isinstance(layout_of(pymux, window), Divided)

        window.zoom = True
        layout = layout_of(pymux, window)

        assert isinstance(layout, Zoomed)
        assert isinstance(layout.inner, Divided)


async def test_zoomed_strip_is_still_strip_underneath():
    """
    The fault Carl asked about. The branch tested `zoom` before
    `strip`, so zooming a column threw the row away for that frame.
    """
    async with create_client(STRIP) as (pymux, draw):
        window, _first, _second = await two_panes(pymux)
        window.zoom = True

        layout = layout_of(pymux, window)

        assert isinstance(layout, Zoomed)
        assert isinstance(layout.inner, Strip)


async def test_zoomed_pane_is_whole_window():
    async with create_client(CHROME) as (pymux, draw):
        window, _first, second = await two_panes(pymux)
        window.zoom = True

        plan = plan_of(pymux, window)

        assert len(plan.rects) == 1
        assert plan.rect_of(second) == plan.bounds
        # The window less the row the title bar hangs in.
        assert plan.bounds.height == ROWS - 2
        assert plan.bounds.width == COLUMNS


async def test_nothing_is_beside_zoomed_pane():
    async with create_client(CHROME) as (pymux, draw):
        window, _first, second = await two_panes(pymux)
        window.zoom = True

        for side in Side:
            assert pane_beside(pymux, window, second, side) is None


async def test_only_zoomed_pane_is_drawn():
    "The other pane is behind it, so no cell of it reaches the screen."
    async with create_client(CHROME) as (pymux, draw):
        window, first, second = await two_panes(pymux)
        first.chosen_name = "hidden"
        second.chosen_name = "shown"

        window.zoom = True
        rows = draw()

        every_cell = "".join(rows.values())
        assert "shown" in every_cell, dump(rows)
        assert "hidden" not in every_cell, dump(rows)


async def test_zoomed_pane_keeps_its_title_bar():
    """
    The row above a pane is the row its bar hangs in, and a zoomed
    pane is drawn inside the same layout as any other, so it has one.
    Before, the zoom branch returned the pane's own container with
    nothing around it, and the float at `top=-1` fell off the screen.
    """
    async with create_client(CHROME) as (pymux, draw):
        window, _first, second = await two_panes(pymux)
        second.chosen_name = "shown"
        window.zoom = True

        rows = draw()

        assert "shown" in rows[0], dump(rows)
        assert " Z " in rows[0], dump(rows)


async def test_mark_goes_when_zoom_does():
    "The same container draws both ways, so the bar has to follow."
    async with create_client(CHROME) as (pymux, draw):
        window, _first, _second = await two_panes(pymux)
        window.zoom = True
        draw()

        window.zoom = False
        rows = draw()

        assert " Z " not in "".join(rows.values()), dump(rows)


async def test_zoomed_stack_pays_for_no_bar_below():
    """
    The bar under a pane names the panes above and below it, and a
    zoomed pane has neither. So the row is not reserved and the pane
    is one row taller.
    """
    async with create_client(CHROME) as (pymux, draw):
        window, _first, _second = await two_panes(pymux, "split-window -v")

        stacked = plan_of(pymux, window).bounds.height
        window.zoom = True
        zoomed = plan_of(pymux, window).bounds.height

        assert zoomed == stacked + 1


async def test_window_comes_back_way_it_was_left():
    "Unzooming is dropping the wrapper, and nothing under it moved."
    async with create_client(STRIP) as (pymux, draw):
        window, first, second = await two_panes(pymux)
        before = plan_of(pymux, window)

        window.zoom = True
        plan_of(pymux, window)
        window.zoom = False
        after = plan_of(pymux, window)

        assert [after.rect_of(pane) for pane in (first, second)] == [before.rect_of(pane) for pane in (first, second)]


async def test_zoomed_window_of_one_pane_is_that_pane():
    "Nothing refuses a zoom of one pane, and nothing needs to."
    async with create_client(CHROME) as (pymux, draw):
        window = pymux.arrangement.get_active_window()
        window.zoom = True

        plan = plan_of(pymux, window)

        assert len(plan.rects) == 1
        assert plan.bounds == plan.rect_of(window.active_pane)


async def test_layout_measures_what_it_is_given():
    "A zoomed pane is the view, whatever size the view is."
    async with create_client(CHROME) as (pymux, draw):
        window, _first, second = await two_panes(pymux)
        window.zoom = True

        plan = layout_of(pymux, window).measure(Size(rows=7, columns=13))

        assert plan.rect_of(second).width == 13
        assert plan.rect_of(second).height == 7
