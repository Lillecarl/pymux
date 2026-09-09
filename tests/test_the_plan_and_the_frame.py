"""
The plan and the frame: they agree, and the plan answers first.

**This is the test that makes slice 2 a probe.** `Strip.measure` works
out where every pane of a strip is, and prompt_toolkit works the same
thing out again while it draws. Two answers to one question is what
Lillecarl/pymux#217 exists to remove, and until `PlanContainer` draws
the plan there is nothing to stop the two drifting apart. This file is
that stop.

A frame records where it drew each pane (`pane_write_positions`), so
the two are directly comparable. They are not in the same coordinates:
the frame is on a client's screen, which the chrome has pushed down and
the strip's own scrolling has pushed sideways. So what is compared is
each pane's size, and its position **up to one offset shared by every
pane**. An offset that is not shared is a real disagreement.
"""

from prompt_toolkit.data_structures import Size
from test_strip_draws import CHROME, a_client

from pymux.layout import the_plan_of

#: Wide enough for two columns of half a window, so a third runs past
#: the edge and the strip has something to scroll.
COLUMNS = 80

#: Deep enough for a stack of two to be worth dividing.
ROWS = 24

STRIP = CHROME + ["set-window-option strip on"]


def a_row_of_panes(pymux, count=3):
    "One column per pane, the way `split-window -h` makes them."
    window = pymux.arrangement.get_active_window()
    panes = [window.active_pane]

    for _ in range(count - 1):
        pymux.handle_command("split-window -h")
        panes.append(window.active_pane)

    return panes


def the_frame(pymux):
    "Where the last frame drew each pane."
    return pymux.get_client_state().layout_manager.pane_write_positions


def the_offsets(pymux, plan):
    """
    How far the frame is from the plan, for each pane.

    One pair for each pane. They have to be all the same pair: that is
    the two coordinate spaces differing by a shift, which is what a
    scrolled strip under a row of chrome is.
    """
    offsets = set()

    for pane, drawn in the_frame(pymux).items():
        rect = plan.rect_of(pane)
        assert (drawn.width, drawn.height) == (rect.width, rect.height), (
            "%r: the frame drew %rx%r and the plan says %rx%r"
            % (pane.name, drawn.width, drawn.height, rect.width, rect.height)
        )
        offsets.add((drawn.xpos - rect.x, drawn.ypos - rect.y))

    return offsets


def the_plan(pymux):
    window = pymux.arrangement.get_active_window()
    return the_plan_of(pymux, window)


def test_the_plan_puts_the_columns_where_the_frame_does():
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        a_row_of_panes(pymux)
        draw()

        assert len(the_offsets(pymux, the_plan(pymux))) == 1


def test_the_plan_divides_a_stack_the_way_the_frame_does():
    """
    The part that is arithmetic rather than order. A stack shares its
    column out by weight, and the cells that do not divide evenly have
    to fall the same way on both sides.
    """
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        a_row_of_panes(pymux, count=2)
        pymux.handle_command("split-window -v")
        pymux.handle_command("split-window -v")
        draw()

        assert len(the_offsets(pymux, the_plan(pymux))) == 1


def test_the_plan_follows_a_resize():
    """
    A person drags a border, and the two still agree.

    The weights carry the answer: a frame writes the real height of
    each pane back into them, and the plan reads them. Two frames,
    because the weights of the frame just drawn are what the next one
    divides by.
    """
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        a_row_of_panes(pymux, count=2)
        pymux.handle_command("split-window -v")
        draw()

        pymux.handle_command("resize-pane -U 3")
        draw()
        draw()

        assert len(the_offsets(pymux, the_plan(pymux))) == 1


def test_the_plan_agrees_about_a_column_that_is_off_the_screen():
    """
    The case the frame alone cannot answer, and the reason for all of
    this. A strip scrolls, so a column can be drawn at a negative
    position -- and the plan, which knows nothing about the screen,
    still has to put it in the same place relative to everything else.
    """
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        panes = a_row_of_panes(pymux)
        state = pymux.get_client_state()

        # A frame first, so that the strip has scrolled to the column
        # the focus is on. Then one step back, which leaves the view
        # where it is because that column is already wholly on screen.
        draw()
        pymux.handle_command("select-pane -L")
        state.sync_focus()
        draw()

        assert the_frame(pymux)[panes[0]].xpos < 0
        assert len(the_offsets(pymux, the_plan(pymux))) == 1


def test_the_plan_uses_the_size_the_window_was_given():
    "So that a client of another size cannot be what it measured."
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        a_row_of_panes(pymux, count=1)
        draw()

        assert pymux.get_window_size() == Size(rows=ROWS - 1, columns=COLUMNS)


# ----------------------------------------------------------------------
# What the frame cannot answer.


def test_a_key_moves_the_focus_before_anything_is_drawn():
    """
    **The frame is not there yet, and the plan is.**

    `select-pane -L` reads where the panes were drawn, so before the
    first frame it found nothing and did nothing: a person who split a
    window and pressed left stayed where they were. A strip answers
    from its plan now, which is worked out and not read back, so the
    key works from the first keystroke.

    This is a change a person sees, and it is what the whole of
    Lillecarl/pymux#217 is for.
    """
    with a_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, _draw):
        panes = a_row_of_panes(pymux)
        window = pymux.arrangement.get_active_window()
        assert window.active_pane is panes[-1]

        pymux.handle_command("select-pane -L")

        assert window.active_pane is panes[-2]
