"""
Where the view sits after the focus moves. Lillecarl/pymux#209.

Carl: "There's still an off-by-one error if you open 3 strips and move
to the rightmost one and then back."

Two properties a strip owes a person, and neither is about arithmetic
for its own sake:

- **A column the focus lands on is wholly on screen.** All of it,
  including the border it owns, because that border is the column's
  right edge and a half-drawn edge reads as a clipped pane.
- **Moving the focus to a column that is already wholly on screen does
  not move the view.** Otherwise walking right and back again leaves
  the strip somewhere it has never been, and every column shifts under
  a person who only changed which pane they were typing in.

The scroll is read off the container, and what is on screen is read off
the cells, because those are two different claims.
"""

import io
import sys
from contextlib import contextmanager

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output

from pymux.main import Pymux
from pymux.plan_container import PlanContainer

ROWS = 12
COLUMNS = 80

STRIP = ["set-option pane-border-status on", "set-window-option strip on"]


class _Connection:
    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


@contextmanager
def a_client(commands=(), rows=ROWS, columns=COLUMNS):
    pymux = Pymux()
    output = Vt100_Output(
        stdout=io.StringIO(), get_size=lambda: Size(rows=rows, columns=columns)
    )
    with create_pipe_input() as pipe:
        state = pymux.add_client(
            output=output,
            input=pipe,
            color_depth=ColorDepth.DEPTH_8_BIT,
            connection=_Connection(),
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


def columns_of(pymux, how_many):
    window = pymux.arrangement.get_active_window()
    opened = [window.active_pane]
    for _ in range(how_many - 1):
        pymux.handle_command("split-window -h")
        opened.append(window.active_pane)
    return window, opened


def the_strip(state):
    "The container that draws the strip of the layout built now."
    found = []

    def walk(container):
        if isinstance(container, PlanContainer):
            found.append(container)
        for child in container.get_children():
            walk(child)

    walk(state.app.layout.container)
    assert len(found) == 1, found
    return found[0]


def the_view(state) -> int:
    "How far along the row the view sits."
    return the_strip(state).offset.x


def where(pymux, pane):
    "Where a pane was drawn, on the screen."
    return pymux.get_client_state().layout_manager.pane_write_positions[pane]


def test_the_whole_walk_left_and_right_and_back():
    """
    Three columns of half an 80 column window: 120 cells of strip seen
    through 80, so the view is either at 0 or at 40 and nowhere else.

    **The repeated numbers are the property.** Two 40s in a row say
    that landing on the middle column from the right moved nothing, and
    two 0s say the same coming from the left. Before, the view went
    40, 38, 0, 1, 40 -- two cells of peek measured against a pane one
    cell narrower than its column. Lillecarl/pymux#209.
    """
    with a_client(STRIP) as (pymux, state, draw):
        window, panes = columns_of(pymux, 3)
        draw()

        walk = [the_view(state)]
        for direction in "LLRRLL":
            move(pymux, state, direction)
            draw()
            walk.append(the_view(state))

        assert walk == [40, 40, 0, 0, 40, 40, 0], walk
        # And the focus really did travel, or the test says nothing.
        assert panes.index(window.active_pane) == 0


def test_the_focus_inside_a_stack_still_finds_its_column():
    """
    A column can be a stack of panes, which is what a niri column is.
    The focused pane is then one of several, and the thing to scroll to
    is still the column.

    Walking the containers answers this for nothing. Reading the
    focused pane's write position got it right only because a pane's
    x-extent happens to equal its column's content, so it was never
    tested and never owned. Lillecarl/pymux#209.
    """
    with a_client(STRIP) as (pymux, state, draw):
        _window, panes = columns_of(pymux, 3)
        draw()
        on_the_third = the_view(state)

        # Split the third column downwards, so it holds two panes.
        pymux.handle_command("split-window -v")
        state.sync_focus()
        draw()

        # Still the same column, so the same view.
        assert the_view(state) == on_the_third, the_view(state)

        # And moving out of the stack and back does not move it either.
        move(pymux, state, "L")
        draw()
        move(pymux, state, "R")
        draw()
        assert the_view(state) == on_the_third, the_view(state)


def test_a_column_wider_than_the_view_still_starts_on_screen():
    """
    It cannot be shown whole, so part of it is off the screen, and the
    part a person is looking at may not be.

    Two thirds of a window is wider than the window once a second
    column is beside it, so this asks for the case rather than
    inventing it.

    **The left edge is the one that is kept**, and it is kept whether
    the column fits or not: Lillecarl/pymux#218. So this holds for a
    column wider than the whole view as well, and
    `test_a_column_wider_than_the_view_shows_its_left_edge`
    (`test_the_strip_plan.py`) reads the offset that says which end it
    is.
    """
    with a_client(STRIP, columns=20) as (pymux, state, draw):
        _window, panes = columns_of(pymux, 2)
        draw()

        # Make the focused column wider than the whole view.
        pymux.handle_command("switch-column-width")
        pymux.handle_command("switch-column-width")
        draw()

        drawn = where(pymux, panes[1])
        # Its left edge is on screen, whatever it costs the right.
        assert drawn.xpos >= 0, drawn
        assert drawn.xpos < 20, drawn


def test_the_focused_column_is_wholly_on_screen():
    """
    Its border is its right edge, and the column is not on screen
    until that is.
    """
    with a_client(STRIP) as (pymux, state, draw):
        _window, panes = columns_of(pymux, 3)
        draw()

        # The focus is on the third column, the one that was just made.
        drawn = where(pymux, panes[2])
        assert drawn.xpos >= 0, drawn
        # The pane, and then the border it owns.
        assert drawn.xpos + drawn.width + 1 <= COLUMNS, drawn


def move(pymux, state, direction):
    """
    Move the focus the way a key moves it.

    **Two steps, and the second is easy to miss.** `select-pane` sets
    `window.active_pane`, and that setter records the pane and nothing
    else. What the strip follows is `layout.current_window`, and
    `ClientState.sync_focus` is what brings that to the active pane --
    bound to `after_key_press`, so a real key does it and a command
    handled directly does not.

    A test that skipped it passed while measuring nothing: the strip
    kept scrolling to the pane that had the focus all along.
    """
    pymux.handle_command("select-pane -%s" % (direction,))
    state.sync_focus()


def test_moving_back_to_a_column_that_is_on_screen_does_not_move_the_view():
    """
    The reported fault. Three columns are wider than the screen, so
    landing on the third scrolls. The second is then wholly on screen,
    and moving to it may not scroll again.
    """
    with a_client(STRIP) as (pymux, state, draw):
        _window, _panes = columns_of(pymux, 3)
        draw()
        on_the_third = the_view(state)

        move(pymux, state, "L")
        draw()

        assert the_view(state) == on_the_third, (on_the_third, the_view(state))


def test_walking_right_and_back_returns_the_same_view():
    """
    The same property, said as a round trip.

    Every move is drawn before the next one, because `select-pane` is
    geometric: it reads where each pane was drawn last time, so two
    moves with no frame between them ask a stale question.
    """
    with a_client(STRIP) as (pymux, state, draw):
        _window, _panes = columns_of(pymux, 3)

        def step(direction):
            move(pymux, state, direction)
            draw()

        # The first frame, so that there are write positions to read.
        # Without it the first move asks an empty question and does
        # nothing, which is the trap this test fell into.
        draw()

        step("L")
        step("L")
        at_the_start = the_view(state)

        for direction in "RRLL":
            step(direction)

        assert the_view(state) == at_the_start, (at_the_start, the_view(state))
