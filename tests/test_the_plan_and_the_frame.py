"""
The plan and the frame: they agree, and the plan answers first.

**This was the test that made slice 2 a probe**, while prompt_toolkit
still divided the row and `Strip.measure` worked the same numbers out a
second time. `PlanContainer` draws the plan now, so the two cannot
drift; what this still holds is the chain from the measurement to the
cells, which is where an offset gets lost or a chrome row gets counted
twice.

A frame records where it drew each pane (`pane_write_positions`), so
the two are directly comparable. They are not in the same coordinates:
the frame is on a client's screen, which the chrome has pushed down and
the strip's own scrolling has pushed sideways. So what is compared is
each pane's size, and its position **up to one offset shared by every
pane**. An offset that is not shared is a real disagreement.
"""

from prompt_toolkit.data_structures import Size
from test_strip_draws import CHROME, create_client

from pymux.layout import plan_of

#: Wide enough for two columns of half a window, so a third runs past
#: the edge and the strip has something to scroll.
COLUMNS = 80

#: Deep enough for a stack of two to be worth dividing.
ROWS = 24

STRIP = CHROME + ["set-window-option strip on"]


def create_row_of_panes(pymux, count=3):
    "One column per pane, the way `split-window -h` makes them."
    window = pymux.arrangement.get_active_window()
    panes = [window.active_pane]

    for _ in range(count - 1):
        pymux.handle_command("split-window -h")
        panes.append(window.active_pane)

    return panes


def frame(pymux):
    "Where the last frame drew each pane."
    return pymux.get_client_state().layout_manager.pane_write_positions


def offsets(pymux, plan):
    """
    How far the frame is from the plan, for each pane.

    One pair for each pane. They have to be all the same pair: that is
    the two coordinate spaces differing by a shift, which is what a
    scrolled strip under a row of chrome is.
    """
    offsets = set()

    for pane, drawn in frame(pymux).items():
        rect = plan.rect_of(pane)
        assert (drawn.width, drawn.height) == (rect.width, rect.height), (
            "%r: the frame drew %rx%r and the plan says %rx%r"
            % (pane.name, drawn.width, drawn.height, rect.width, rect.height)
        )
        offsets.add((drawn.xpos - rect.x, drawn.ypos - rect.y))

    return offsets


def test_the_plan_puts_the_columns_where_the_frame_does():
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        create_row_of_panes(pymux)
        draw()

        assert len(offsets(pymux, plan_of(pymux, pymux.arrangement.get_active_window()))) == 1


def test_the_plan_divides_a_stack_the_way_the_frame_does():
    """
    The part that is arithmetic rather than order. A stack shares its
    column out by weight, and the cells that do not divide evenly have
    to fall the same way on both sides.
    """
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        create_row_of_panes(pymux, count=2)
        pymux.handle_command("split-window -v")
        pymux.handle_command("split-window -v")
        draw()

        assert len(offsets(pymux, plan_of(pymux, pymux.arrangement.get_active_window()))) == 1


def test_the_plan_follows_a_resize():
    """
    A person drags a border, and the frame follows.

    The weights carry the answer, and the resize writes them: it
    measures the plan, puts the cells each pane holds into its weight,
    and then applies the delta, so one row asked for is one row given.
    """
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        panes = create_row_of_panes(pymux, count=2)
        pymux.handle_command("split-window -v")
        draw()

        was = plan_of(pymux, pymux.arrangement.get_active_window()).rect_of(panes[-1]).height
        pymux.handle_command("resize-pane -U 3")
        draw()

        assert plan_of(pymux, pymux.arrangement.get_active_window()).rect_of(panes[-1]).height == was - 3
        assert len(offsets(pymux, plan_of(pymux, pymux.arrangement.get_active_window()))) == 1


def test_a_divided_window_is_drawn_where_its_plan_says_too():
    "The layout pymux uses unless a person asks for something else."
    with create_client(CHROME, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        create_row_of_panes(pymux, count=2)
        pymux.handle_command("split-window -v")
        draw()

        assert len(offsets(pymux, plan_of(pymux, pymux.arrangement.get_active_window()))) == 1


def test_the_plan_holds_a_column_the_frame_never_drew():
    """
    The case the frame alone cannot answer, and the reason for all of
    this. A strip scrolls, so a column can be off the screen -- and the
    plan, which knows nothing about the screen, still puts it in the
    same place relative to everything else.

    The frame does not hold it at all: no part of it is in the view, so
    it is not drawn (Lillecarl/pymux#224). Everything that asks where
    that pane is asks the plan.
    """
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        panes = create_row_of_panes(pymux)
        state = pymux.get_client_state()

        # A frame first, so that the strip has scrolled to the column
        # the focus is on. Then one step back, which leaves the view
        # where it is because that column is already wholly on screen.
        draw()
        pymux.handle_command("select-pane -L")
        state.sync_focus()
        draw()

        plan = plan_of(pymux, pymux.arrangement.get_active_window())

        assert panes[0] not in frame(pymux)
        # And the plan puts it left of the column that is drawn.
        assert plan.rect_of(panes[0]).right <= plan.rect_of(panes[1]).x
        assert len(offsets(pymux, plan)) == 1


def test_the_plan_uses_the_size_the_window_was_given():
    "So that a client of another size cannot be what it measured."
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, draw):
        create_row_of_panes(pymux, count=1)
        draw()

        assert pymux.size_of_the_plane() == Size(rows=ROWS - 1, columns=COLUMNS)


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
    with create_client(STRIP, rows=ROWS, columns=COLUMNS) as (pymux, _draw):
        panes = create_row_of_panes(pymux)
        window = pymux.arrangement.get_active_window()
        assert window.active_pane is panes[-1]

        pymux.handle_command("select-pane -L")

        assert window.active_pane is panes[-2]
