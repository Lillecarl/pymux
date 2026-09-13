"""
`choose-window`: prefix w opens a bar across the top of the screen,
and what is under it is the window it points at, running.

The rules this judges, Lillecarl/pymux#295 and Lillecarl/pymux#327:

- The bar sits at the very top and covers as few rows as its entries
  need. The windows flow across it like words and wrap.
- **There is no picture.** Moving the point switches this client to
  that window, so a person reads the real thing at full size. Escape
  puts the client back where it started; Enter leaves it where it is.
- h and l walk the entries, j and k the lines they wrapped onto, and
  the arrows go with them. The ends hold.
- `/` searches: what is typed narrows the entries to the names, the
  indexes or the session that carry it, and the client follows to the
  first match. Enter takes it and Escape brings the keys back.
- An entry answers a click, the way a column of the strip does.
- With a command as its argument, the chooser runs the command on the
  chosen window instead of moving, and puts the client back first: the
  command is what the person asked for and being moved is not.
"""

from prompt_toolkit.application.current import set_app
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen as PtScreen, WritePosition
from prompt_toolkit.mouse_events import MouseEventType

from pyte.streams import Stream

from session import create_session

#: prompt_toolkit keeps the keys it can name as `Keys` members and
#: the printable ones as themselves.
_KEYS = {
    "up": Keys.Up,
    "down": Keys.Down,
    "left": Keys.Left,
    "right": Keys.Right,
    "enter": Keys.Enter,
    "escape": Keys.Escape,
}


class _Click:
    "The one mouse event an entry answers."
    event_type = MouseEventType.MOUSE_DOWN


def fire(state, key: str) -> None:
    """
    The builtin binding that answers this key while the chooser
    shows.

    The handlers are closures of `_load_builtins`, so the registry
    is the way to reach one: among the bindings that match the key
    and hold under the conditions now, the last one added wins,
    which is prompt_toolkit's own rule.
    """
    wanted = _KEYS.get(key, key)
    with set_app(state.app):
        answers = [
            b
            for b in state.pymux.key_bindings_manager.key_bindings.bindings
            if b.keys == (wanted,) and b.filter()
        ]
        assert answers, "no binding answered %r" % (key,)
        # The handler asks the app for its client state, so it runs
        # while the app is current.
        answers[-1].handler(None)


def here(pymux, state):
    "The window this client is looking at now."
    with set_app(state.app):
        return pymux.arrangement.get_active_window()


def entries(state) -> list:
    "What the bar says, entry by entry."
    with set_app(state.app):
        return state.layout_manager.chooser_entries()


def bar_text(state) -> str:
    "The bar as it draws, newlines and all."
    with set_app(state.app):
        return fragment_list_to_text(state.layout_manager._choose_window_tokens())


def _bar_float(state):
    "The float the window bar draws in."
    for one in state.layout_manager.layout.floats:
        content = getattr(one.content, "content", None)
        builder = getattr(content, "get_container", None)
        if builder is not None and builder.__name__ == "_window_bar":
            return one
    raise AssertionError("the layout draws no window bar at all")


# ----------------------------------------------------------------------
# Where it draws.


async def test_the_bar_is_at_the_very_top():
    async with create_session() as (pymux, state):
        one = _bar_float(state)
        assert one.top == 0
        assert one.left == 0 and one.right == 0
        # And no bottom: it takes the rows its entries need and no
        # more, because the window is underneath it.
        assert one.bottom is None

        with set_app(state.app):
            assert not one.content.filter()
            pymux.handle_command("choose-window")
            assert one.content.filter()


async def test_the_entries_flow_across_the_bar():
    "One line while they fit, and no row of its own for each."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        assert len(entries(state)) == len(pymux.arrangement.windows)
        assert "\n" not in bar_text(state)


async def test_the_entries_wrap_when_the_line_is_full():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            for number in range(8):
                pymux.handle_command("new-window")
                pymux.handle_command("rename-window a-long-window-name-%i" % number)
            pymux.handle_command("choose-window")

            width = state.layout_manager._bar_width()
            lines = state.layout_manager.chooser_lines(width)

        assert len(lines) > 1, "eight long names fitted on one line"
        assert bar_text(state).count("\n") == len(lines) - 1

        labels = entries(state)
        # Nothing is dropped, and nothing is on two lines.
        assert sorted(i for line in lines for i in line) == list(range(len(labels)))
        for line in lines:
            assert sum(len(labels[i]) for i in line) + len(line) - 1 <= width


async def test_the_bar_takes_the_rows_its_entries_need():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-window")
            rows = state.layout_manager._bar_rows.height()

        # One window, so one line. The search line is under it.
        assert rows.preferred == 1


# ----------------------------------------------------------------------
# The preview is the switch.


async def test_moving_the_point_switches_this_client():
    "The whole of the preview: a person reads the window itself."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        first, second = pymux.arrangement.windows[-2:]
        assert here(pymux, state) is second

        fire(state, "h")
        assert here(pymux, state) is first

        fire(state, "l")
        assert here(pymux, state) is second


async def test_the_ends_hold():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        last = len(pymux.arrangement.windows) - 1
        fire(state, "l")
        assert state.choose_window_index == last

        fire(state, "right")
        assert state.choose_window_index == last

        fire(state, "h")
        fire(state, "h")
        fire(state, "left")
        assert state.choose_window_index == 0


async def test_j_and_k_move_a_line_and_keep_the_place_along_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            for number in range(8):
                pymux.handle_command("new-window")
                pymux.handle_command("rename-window a-long-window-name-%i" % number)
            pymux.handle_command("choose-window")
            lines = state.layout_manager.chooser_lines(
                state.layout_manager._bar_width()
            )

            # Onto the second entry of the first line, then down.
            state.layout_manager.point_at(lines[0][1])

        fire(state, "j")
        assert state.choose_window_index == lines[1][min(1, len(lines[1]) - 1)]

        fire(state, "k")
        assert state.choose_window_index == lines[0][1]


async def test_escape_goes_back_to_the_window_it_started_on():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        started_on = here(pymux, state)
        fire(state, "h")
        assert here(pymux, state) is not started_on

        fire(state, "escape")

        assert not state.choose_window
        assert here(pymux, state) is started_on


async def test_q_goes_back_as_well():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        started_on = here(pymux, state)
        fire(state, "h")
        fire(state, "q")

        assert not state.choose_window
        assert here(pymux, state) is started_on


async def test_enter_keeps_the_window_the_point_is_on():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        fire(state, "h")
        chosen = here(pymux, state)
        fire(state, "enter")

        assert not state.choose_window
        assert here(pymux, state) is chosen


async def test_a_star_marks_the_window_escape_goes_back_to():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        marked = [label for label in entries(state) if label.endswith("*")]
        assert len(marked) == 1

        # And it stays on the one it started from, not on the one the
        # point has moved to.
        fire(state, "h")
        assert [label for label in entries(state) if label.endswith("*")] == marked


# ----------------------------------------------------------------------
# The search.


async def test_search_narrows_and_the_client_follows():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("rename-window needle")
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

            state.choose_window_filter.insert_text("needle")

        needle = next(w for w in pymux.arrangement.windows if w.name == "needle")
        assert entries(state) == ["%s:needle" % (needle.index,)]
        assert state.choose_window_index == 0
        assert here(pymux, state).name == "needle"


async def test_enter_from_search_takes_the_first_match():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("rename-window needle")
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

            state.choose_window_filter.insert_text("needle")

        fire(state, "enter")

        assert not state.choose_window
        assert here(pymux, state).name == "needle"


async def test_search_that_matches_nothing_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-window")
            state.choose_window_filter.insert_text("no window is numbered 99")

        assert "No window matches." in bar_text(state)


async def test_escape_in_the_search_keeps_the_chooser():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        # The search has to have the focus for Escape to mean "leave
        # the search"; `/` is what gives it that.
        fire(state, "/")
        with set_app(state.app):
            state.choose_window_filter.insert_text("1")

        fire(state, "escape")

        assert state.choose_window, "the search took the chooser with it"
        assert state.choose_window_filter.text == ""


# ----------------------------------------------------------------------
# The rest of the surface.


async def test_prefix_w_binding_opens_it():
    "The tmux key: prefix w opens the chooser, as it opens the tree."
    async with create_session() as (pymux, state):
        assert ("w", "choose-window") in (
            pymux.key_bindings_manager.prefix_keys()
        )


async def test_click_on_an_entry_takes_its_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        tokens = [
            token
            for token in state.layout_manager._choose_window_tokens()
            if len(token) == 3
        ]
        handler = tokens[0][2]

        # The click reaches the handler with the client's app
        # current, as the renderer's call does.
        with set_app(state.app):
            assert handler(_Click()) is None

        assert not state.choose_window
        assert here(pymux, state) is pymux.arrangement.windows[0]


async def test_template_runs_on_chosen_window_and_leaves_the_client():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window \"kill-window -t '%%'\"")

        started_on = here(pymux, state)
        fire(state, "h")
        chosen = here(pymux, state)
        fire(state, "enter")

        assert not state.choose_window
        assert chosen not in pymux.arrangement.windows
        # The command is what was asked for. Being moved was not.
        assert here(pymux, state) is started_on


def _drawn(state) -> list:
    """
    Every row of this client's screen, as a string.

    At the size the client says it is: the bar reads that size to wrap
    its entries, so a render at any other one draws a bar that does not
    fit what it wrapped.
    """
    rows, columns = state.app.output.get_size()
    screen = PtScreen()
    with set_app(state.app):
        state.app.layout.container.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(xpos=0, ypos=0, width=columns, height=rows),
            "",
            False,
            None,
        )
        screen.draw_all_floats()
    return [
        "".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
        for y in range(rows)
    ]


async def test_the_window_draws_under_the_bar():
    """
    The shape, read off a drawn screen: the entries on the first row,
    the search under them, and the window itself below that.

    The bar covers the top rows of the window it is showing, which is
    what "on top" means and what tmux's tree costs as well. So the
    output this looks for is written further down the pane.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("rename-window needle")
            pymux.handle_command("choose-window")

        window = here(pymux, state)

        # A pane's screen has no size until a render gives it one, and
        # text written to a screen of no width wraps one character to a
        # row. Sizing the control sizes the screen with it.
        # Lillecarl/pymux#321.
        window.active_pane.terminal.set_size(78, 18)
        Stream(window.active_pane.screen).feed("\r\n" * 5 + "a line of output")

        rows = _drawn(state)

        assert "needle" in rows[0]
        assert rows[1].strip().startswith("/")

        showing = [i for i, text in enumerate(rows) if "a line of output" in text]
        assert showing, "the window did not draw under the bar:\n" + "\n".join(
            "|" + r for r in rows
        )
        assert min(showing) > 1
