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

from prompt_toolkit.data_structures import Size
from test_strip_draws import CHROME, a_client, a_dump

from pymux.divided import Divided
from pymux.layout import the_layout_of, the_pane_beside, the_plan_of
from pymux.plane import Side
from pymux.strip import Strip
from pymux.zoomed import Zoomed

ROWS, COLUMNS = 12, 40

STRIP = CHROME + ["set-window-option strip on"]


def two_panes(pymux, command="split-window -h"):
    "Two panes in the active window, and the second has the focus."
    window = pymux.arrangement.get_active_window()
    first = window.active_pane
    pymux.handle_command(command)
    return window, first, window.active_pane


def test_zoom_wraps_the_layout_and_keeps_it():
    with a_client(CHROME) as (pymux, draw):
        window, _first, _second = two_panes(pymux)

        assert isinstance(the_layout_of(pymux, window), Divided)

        window.zoom = True
        layout = the_layout_of(pymux, window)

        assert isinstance(layout, Zoomed)
        assert isinstance(layout.inner, Divided)


def test_a_zoomed_strip_is_still_a_strip_underneath():
    """
    The fault Carl asked about. The branch tested `zoom` before
    `strip`, so zooming a column threw the row away for that frame.
    """
    with a_client(STRIP) as (pymux, draw):
        window, _first, _second = two_panes(pymux)
        window.zoom = True

        layout = the_layout_of(pymux, window)

        assert isinstance(layout, Zoomed)
        assert isinstance(layout.inner, Strip)


def test_the_zoomed_pane_is_the_whole_window():
    with a_client(CHROME) as (pymux, draw):
        window, _first, second = two_panes(pymux)
        window.zoom = True

        plan = the_plan_of(pymux, window)

        assert len(plan.rects) == 1
        assert plan.rect_of(second) == plan.plane
        # The window less the row the title bar hangs in.
        assert plan.plane.height == ROWS - 2
        assert plan.plane.width == COLUMNS


def test_nothing_is_beside_a_zoomed_pane():
    with a_client(CHROME) as (pymux, draw):
        window, _first, second = two_panes(pymux)
        window.zoom = True

        for side in Side:
            assert the_pane_beside(pymux, window, second, side) is None


def test_only_the_zoomed_pane_is_drawn():
    "The other pane is behind it, so no cell of it reaches the screen."
    with a_client(CHROME) as (pymux, draw):
        window, first, second = two_panes(pymux)
        first.chosen_name = "hidden"
        second.chosen_name = "shown"

        window.zoom = True
        rows = draw()

        every_cell = "".join(rows.values())
        assert "shown" in every_cell, a_dump(rows)
        assert "hidden" not in every_cell, a_dump(rows)


def test_a_zoomed_pane_keeps_its_title_bar():
    """
    The row above a pane is the row its bar hangs in, and a zoomed
    pane is drawn inside the same layout as any other, so it has one.
    Before, the zoom branch returned the pane's own container with
    nothing around it, and the float at `top=-1` fell off the screen.
    """
    with a_client(CHROME) as (pymux, draw):
        window, _first, second = two_panes(pymux)
        second.chosen_name = "shown"
        window.zoom = True

        rows = draw()

        assert "shown" in rows[0], a_dump(rows)
        assert " Z " in rows[0], a_dump(rows)


def test_the_mark_goes_when_the_zoom_does():
    "The same container draws both ways, so the bar has to follow."
    with a_client(CHROME) as (pymux, draw):
        window, _first, _second = two_panes(pymux)
        window.zoom = True
        draw()

        window.zoom = False
        rows = draw()

        assert " Z " not in "".join(rows.values()), a_dump(rows)


def test_a_zoomed_stack_pays_for_no_bar_below():
    """
    The bar under a pane names the panes above and below it, and a
    zoomed pane has neither. So the row is not reserved and the pane
    is one row taller.
    """
    with a_client(CHROME) as (pymux, draw):
        window, _first, _second = two_panes(pymux, "split-window -v")

        stacked = the_plan_of(pymux, window).plane.height
        window.zoom = True
        zoomed = the_plan_of(pymux, window).plane.height

        assert zoomed == stacked + 1


def test_the_window_comes_back_the_way_it_was_left():
    "Unzooming is dropping the wrapper, and nothing under it moved."
    with a_client(STRIP) as (pymux, draw):
        window, first, second = two_panes(pymux)
        before = the_plan_of(pymux, window)

        window.zoom = True
        the_plan_of(pymux, window)
        window.zoom = False
        after = the_plan_of(pymux, window)

        assert [after.rect_of(pane) for pane in (first, second)] == [
            before.rect_of(pane) for pane in (first, second)
        ]


def test_a_zoomed_window_of_one_pane_is_that_pane():
    "Nothing refuses a zoom of one pane, and nothing needs to."
    with a_client(CHROME) as (pymux, draw):
        window = pymux.arrangement.get_active_window()
        window.zoom = True

        plan = the_plan_of(pymux, window)

        assert len(plan.rects) == 1
        assert plan.plane == plan.rect_of(window.active_pane)


def test_the_layout_measures_what_it_is_given():
    "A zoomed pane is the view, whatever size the view is."
    with a_client(CHROME) as (pymux, draw):
        window, _first, second = two_panes(pymux)
        window.zoom = True

        plan = the_layout_of(pymux, window).measure(Size(rows=7, columns=13))

        assert plan.rect_of(second).width == 13
        assert plan.rect_of(second).height == 7
