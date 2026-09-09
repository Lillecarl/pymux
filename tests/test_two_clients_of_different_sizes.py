"""
Two clients, two sizes, one window. Slice 5 of Lillecarl/pymux#217.

**A plane is shared and a view is not.** A pane has one pty, so it has
one size however many clients look at it -- decision 10 of
`docs/layout-engine-plan.md`. `window-size` says whose terminal that
one size comes from, and each client then sees as much of the plane as
it can through a view of its own.

- `smallest`, the default and what pymux always did: every client sees
  the whole window, and a bigger one draws background around it.
- `largest`: the window is the biggest client's, and a smaller one
  moves its view over it. tmux has the same option and leaves its
  smaller client stuck at the top left of the window.
- `latest`: the window belongs to whichever terminal somebody last
  typed in.
- `manual`: a size a person named with `resize-window`, and no client
  changes it.

This is the only file with two clients of different sizes in it, and
everything slice 5 did is judged here. The rest of the suite has one
client, where the plane and the view are the same rectangle and every
question about the difference is unaskable.
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

from pymux.enums import WindowSize
from pymux.main import Pymux
from pymux.plan_container import PlanContainer

#: One client with room, and one without. The columns differ by enough
#: that a pane of the big plane cannot fit in the small client.
BIG = Size(rows=24, columns=100)
SMALL = Size(rows=12, columns=40)


class _Connection:
    "Two clients need two of these: the server keys on the connection."

    kitty_source_flags = 0
    pointer_shape = None
    graphics = None

    def set_pointer_shape(self, shape):
        pass

    def _send_packet(self, packet):
        pass


class _Client:
    "A client, and the frame it draws."

    def __init__(self, pymux, state, size) -> None:
        self.pymux = pymux
        self.state = state
        self.size = size

    def draw(self) -> Screen:
        with set_app(self.state.app):
            screen = Screen()
            self.state.app.layout.container.write_to_screen(
                screen,
                MouseHandlers(),
                WritePosition(
                    xpos=0, ypos=0, width=self.size.columns, height=self.size.rows
                ),
                "",
                False,
                None,
            )
            screen.draw_all_floats()
            self.state.app.renderer._last_screen = screen
            return screen

    @property
    def panes(self) -> PlanContainer:
        "The container that drew this client's panes."
        found = []

        def walk(container):
            if isinstance(container, PlanContainer):
                found.append(container)
            for child in container.get_children():
                walk(child)

        with set_app(self.state.app):
            walk(self.state.app.layout.container)

        assert len(found) == 1, found
        return found[0]

    @property
    def view(self):
        return self.panes.view

    def run(self, command: str) -> None:
        with set_app(self.state.app):
            self.pymux.handle_command(command)
            self.state.sync_focus()


@contextmanager
def two_clients(commands=()):
    """
    A session with a big client and a small one, both on one window.

    The commands run on the big client, which is the one that opens
    the window, so both are watching it.
    """
    pymux = Pymux()
    opened = []

    with create_pipe_input() as pipe:

        def attach(size):
            output = Vt100_Output(stdout=io.StringIO(), get_size=lambda: size)
            state = pymux.add_client(
                output=output,
                input=pipe,
                color_depth=ColorDepth.DEPTH_8_BIT,
                connection=_Connection(),
            )
            client = _Client(pymux, state, size)
            opened.append(client)
            return client

        big = attach(BIG)
        with set_app(big.state.app):
            for command in commands:
                pymux.handle_command(command)

        small = attach(SMALL)

        try:
            yield pymux, big, small
        finally:
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


def the_window(pymux):
    return pymux.arrangement.windows[0]


def the_plane(pymux):
    "How big the plane of the one window is."
    return pymux.the_size_of_the_plane(the_window(pymux))


# ----------------------------------------------------------------------
# Which client decides.


def test_the_smallest_client_decides_by_default():
    "Which is what pymux always did, and now it is an option."
    with two_clients() as (pymux, _big, _small):
        assert the_plane(pymux).columns == SMALL.columns
        assert the_plane(pymux).rows == SMALL.rows - 1  # The status line.


def test_the_largest_client_decides_when_it_is_asked_to():
    with two_clients(["set-window-option window-size largest"]) as (pymux, _b, _s):
        assert the_plane(pymux).columns == BIG.columns
        assert the_plane(pymux).rows == BIG.rows - 1


def test_the_latest_client_to_be_used_decides():
    """
    What a person with a laptop and a desktop on one session wants:
    the terminal they are typing in gets the window.

    Attaching counts as using, so the small client -- which attached
    second -- holds the window until somebody touches the big one.
    """
    with two_clients(["set-window-option window-size latest"]) as (pymux, big, small):
        assert the_plane(pymux).columns == SMALL.columns

        pymux.a_client_was_used(big.state)
        assert the_plane(pymux).columns == BIG.columns

        pymux.a_client_was_used(small.state)
        assert the_plane(pymux).columns == SMALL.columns


def test_a_key_press_is_what_uses_a_client():
    """
    The wiring, which is what `latest` rests on: a key press on a
    client stamps it. The event is fired rather than a key fed,
    because feeding one starts a timeout task and this test has no
    loop; what is judged is that the handler is on the event.
    """
    with two_clients(["set-window-option window-size latest"]) as (pymux, big, small):
        before = big.state.last_used
        big.state.app.key_processor.before_key_press.fire()

        assert big.state.last_used > before
        assert big.state.last_used > small.state.last_used
        assert the_plane(pymux).columns == BIG.columns


def test_a_manual_size_follows_no_client():
    """
    **The size is the window's own**, so no status row comes off it.
    `-x 100 -y 40` is a hundred cells by forty, and neither client
    changes it.
    """
    with two_clients() as (pymux, big, _small):
        big.run("resize-window -x 120 -y 40")

        assert the_window(pymux).window_size is WindowSize.MANUAL
        assert the_plane(pymux) == Size(rows=40, columns=120)


def test_an_axis_that_is_not_given_keeps_what_it_had():
    with two_clients() as (pymux, big, _small):
        big.run("resize-window -x 120")

        assert the_plane(pymux).columns == 120
        assert the_plane(pymux).rows == SMALL.rows - 1


def test_manual_with_no_size_freezes_the_window_as_it_is():
    """
    A person who says `manual` and nothing else means "stop following
    the clients", not "pick a size for me". tmux does the same.
    """
    with two_clients() as (pymux, big, _small):
        was = the_plane(pymux)
        big.run("set-window-option window-size manual")

        assert the_plane(pymux) == was
        # And it stays there when a client would have changed it.
        big.run("set-window-option window-size manual")
        assert the_plane(pymux) == was


def test_a_nudge_counts_from_the_size_the_window_has_now():
    """
    Which is the whole point of a nudge, and why it is the one a
    person binds to a key: an absolute size means knowing the size
    first. Lillecarl/pymux#225.
    """
    with two_clients() as (pymux, big, _small):
        was = the_plane(pymux)
        big.run("resize-window -R 10 -D 4")

        assert the_window(pymux).window_size is WindowSize.MANUAL
        assert the_plane(pymux) == Size(rows=was.rows + 4, columns=was.columns + 10)


def test_the_other_two_nudges_go_the_other_way():
    with two_clients() as (pymux, big, _small):
        was = the_plane(pymux)
        big.run("resize-window -L 3 -U 2")

        assert the_plane(pymux) == Size(rows=was.rows - 2, columns=was.columns - 3)


def test_an_absolute_size_and_a_nudge_are_read_in_that_order():
    with two_clients() as (pymux, big, _small):
        big.run("resize-window -x 80 -R 10")

        assert the_plane(pymux).columns == 90


def test_a_nudge_stops_at_one_cell_and_does_not_complain():
    """
    A key held down at the edge does nothing, the way it does nothing
    in `move-column`. An absolute size below one is a different thing:
    a person asking for something that cannot exist, and that raises.
    """
    with two_clients() as (pymux, big, _small):
        big.run("resize-window -L 500 -U 500")

        assert big.state.message is None
        assert the_plane(pymux) == Size(rows=1, columns=1)


def test_a_window_bigger_than_every_client_is_still_reachable():
    """
    Which is what makes a manual size safe here and awkward in tmux.
    Both clients scroll their own view over a window neither can show.
    """
    with two_clients(["resize-window -x 200 -y 40"]) as (pymux, big, small):
        big.run("split-window -h")
        big.draw()
        small.draw()

        _left, right = the_window(pymux).panes

        for client in (big, small):
            assert client.view.size.columns < the_plane(pymux).columns
            client.run("select-pane -R")
            client.draw()
            assert client.view.shows(client.panes.plan.rect_of(right))


def test_a_size_that_is_not_a_number_is_refused():
    with two_clients() as (pymux, big, _small):
        was = the_plane(pymux)
        big.run("resize-window -x wide")

        assert big.state.message is not None
        assert the_plane(pymux) == was


def test_a_window_of_no_cells_is_refused():
    with two_clients() as (pymux, big, _small):
        big.run("resize-window -x 0")

        assert big.state.message is not None
        assert the_window(pymux).window_size is WindowSize.SMALLEST


def test_a_word_that_is_not_a_policy_is_refused():
    with two_clients() as (pymux, big, _small):
        with set_app(big.state.app):
            pymux.handle_command("set-window-option window-size enormous")
            assert big.state.message is not None

        assert the_plane(pymux).columns == SMALL.columns


def test_the_policy_belongs_to_one_window():
    """
    Two windows of a session can be watched by different clients, so
    the policy is a window option and a new window starts on the
    default.
    """
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, _s):
        big.run("new-window")

        first, second = pymux.arrangement.windows
        assert first.window_size is WindowSize.LARGEST
        assert second.window_size is WindowSize.SMALLEST


def test_a_default_says_what_every_new_window_starts_with():
    "Which is what `-g` is for, and it changes no window that is open."
    with two_clients() as (pymux, big, _small):
        big.run("set-window-option -g window-size largest")

        first = the_window(pymux)
        big.run("new-window")
        _, second = pymux.arrangement.windows

        assert first.window_size is WindowSize.SMALLEST
        assert second.window_size is WindowSize.LARGEST


# ----------------------------------------------------------------------
# What each client then sees.


def test_the_plan_is_the_same_for_both_clients():
    """
    **The plan is shared, never per client.** A pane has one pty, so
    the rectangle it is drawn in is one rectangle, and only the part of
    it each client can see differs. Decision 10.
    """
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, small):
        big.run("split-window -h")
        big.draw()
        small.draw()

        assert big.panes.measured_for == small.panes.measured_for
        assert big.panes.plan.rects.values().__len__() == 2

        for pane in the_window(pymux).panes:
            assert big.panes.plan.rect_of(pane) == small.panes.plan.rect_of(pane)


def test_a_client_smaller_than_the_plane_sees_part_of_it():
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, small):
        big.draw()
        small.draw()

        assert big.view.size.columns == BIG.columns
        assert small.view.size.columns == SMALL.columns
        assert small.view.size.columns < the_plane(pymux).columns


def test_the_small_client_scrolls_to_the_pane_it_is_on():
    """
    The reason `largest` is worth having. tmux leaves a client too
    small to see the whole window at its top left; here the view moves,
    so every pane is reachable from the small terminal as well.
    """
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, small):
        big.run("split-window -h")
        big.draw()
        small.draw()

        left, right = the_window(pymux).panes
        assert small.view.offset.x == 0

        small.run("select-pane -R")
        small.draw()

        assert small.view.offset.x == small.panes.plan.rect_of(right).x
        assert small.view.shows(small.panes.plan.rect_of(right))
        assert not small.view.shows(small.panes.plan.rect_of(left))


def test_the_big_client_does_not_move_when_the_small_one_scrolls():
    "A view belongs to one client. Nothing a person does moves another's."
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, small):
        big.run("split-window -h")
        big.draw()
        small.draw()

        small.run("select-pane -R")
        small.draw()
        big.draw()

        assert big.view.offset.x == 0
        # And the big client still shows every pane.
        for pane in the_window(pymux).panes:
            assert big.view.shows(big.panes.plan.rect_of(pane))


def test_a_pane_is_sized_for_the_plane_and_not_for_a_client():
    """
    One pty, one size. The program in a pane writes for the plane, and
    the small client shows as much of that as it has room for.
    """
    with two_clients(["set-window-option window-size largest"]) as (pymux, big, small):
        big.draw()
        small.draw()

        (pane,) = the_window(pymux).panes
        assert big.panes.plan.rect_of(pane).width == the_plane(pymux).columns
        assert pane.screen.columns == the_plane(pymux).columns


# ----------------------------------------------------------------------
# And the default policy is unchanged.


def test_under_smallest_every_client_sees_the_whole_plane():
    with two_clients() as (pymux, big, small):
        big.run("split-window -h")
        big.draw()
        small.draw()

        for client in (big, small):
            for pane in the_window(pymux).panes:
                assert client.view.shows(client.panes.plan.rect_of(pane))
            assert client.view.offset.x == 0
