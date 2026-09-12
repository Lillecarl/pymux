"""
`choose-window`: prefix w opens a chooser over the windows of the
session, and Enter switches to the window the chooser points at.

The rules this judges, Lillecarl/pymux#295: it opens on the window
this client looks at, the keys move the point and clamp at the ends,
Enter switches and closes, and q and Escape close without switching.
`/` searches: what is typed narrows the list to the names or indexes
that carry it, Enter takes the first match, and Escape brings the
keys back. With a command as its argument, the chooser runs the
command on the chosen window instead of switching, with `%%` for its
target. A row answers a click, the way a column of the strip does.
"""

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseEventType

from session import create_session, in_a_loop

#: prompt_toolkit keeps the keys it can name as `Keys` members and
#: the printable ones as themselves.
_KEYS = {
    "up": Keys.Up,
    "down": Keys.Down,
    "enter": Keys.Enter,
    "escape": Keys.Escape,
}


class _Click:
    "The one mouse event a row answers."
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


@in_a_loop
async def test_the_command_opens_the_chooser_on_the_current_window():
    "The chooser opens on the window this client looks at."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        assert state.choose_window
        rows = state.layout_manager._choose_window_tokens()
        assert len(rows) == len(pymux.arrangement.windows)
        # The last window made is the one the client looks at.
        assert rows[-1][0] == "class:chooser.selected"


@in_a_loop
async def test_the_chooser_draws_in_a_float():
    "A box on the view, the inset of the keys pop-up."
    async with create_session() as (pymux, state):
        boxes = [
            one
            for one in state.layout_manager.layout.floats
            if getattr(getattr(one.content, "content", None), "get_container", None)
            is not None
            and one.content.content.get_container.__name__ == "_choose_window_box"
        ]
        assert boxes, "the layout draws no chooser at all"

        with set_app(state.app):
            pymux.handle_command("choose-window")

        assert boxes[0].content.filter()


@in_a_loop
async def test_the_keys_move_and_clamp():
    "j and k with the arrows; the ends hold."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        last = len(pymux.arrangement.windows) - 1
        fire(state, "down")
        assert state.choose_window_index == last, "the last row did not hold"

        fire(state, "j")
        assert state.choose_window_index == last

        fire(state, "k")
        assert state.choose_window_index == last - 1

        fire(state, "k")
        fire(state, "up")
        assert state.choose_window_index == 0, "the first row did not hold"


@in_a_loop
async def test_enter_switches_and_closes():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        # The chooser opens on the last row; walk it to the first.
        for _ in pymux.arrangement.windows:
            fire(state, "up")
        fire(state, "enter")

        assert not state.choose_window
        assert (
            pymux.arrangement.get_active_window() is pymux.arrangement.windows[0]
        )


@in_a_loop
async def test_q_and_escape_close_without_switching():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        fire(state, "q")
        assert not state.choose_window
        with set_app(state.app):
            assert (
                pymux.arrangement.get_active_window()
                is pymux.arrangement.windows[-1]
            )

        with set_app(state.app):
            pymux.handle_command("choose-window")
        fire(state, "escape")
        assert not state.choose_window
        with set_app(state.app):
            assert (
                pymux.arrangement.get_active_window()
                is pymux.arrangement.windows[-1]
            )


@in_a_loop
async def test_the_prefix_w_binding_opens_it():
    "The tmux key: prefix w opens the chooser, as it opens the tree."
    async with create_session() as (pymux, state):
        assert ("w", "choose-window") in (
            pymux.key_bindings_manager.keys_a_prefix_leads_to()
        )


@in_a_loop
async def test_the_search_narrows_the_rows_to_what_was_typed():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("rename-window needle")
            pymux.handle_command("choose-window")

            # The panes of the harness name themselves, so the search
            # reaches the windows by index; a name that carries the
            # text narrows the same way. The first window is index 1.
            state.choose_window_filter.insert_text("1")

        rows = state.layout_manager._choose_window_tokens()
        assert len(rows) == 1
        assert "1" in rows[0][1]
        # Typing puts the point back on the first match.
        assert state.choose_window_index == 0


@in_a_loop
async def test_enter_from_the_search_takes_the_first_match():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

            state.choose_window_filter.insert_text("1")

        # The point rests on the only match; Enter, typed into the
        # search, takes it. The first window is index 1.
        fire(state, "enter")

        assert not state.choose_window
        with set_app(state.app):
            assert (
                pymux.arrangement.get_active_window()
                is pymux.arrangement.windows[0]
            )


@in_a_loop
async def test_a_search_that_matches_nothing_says_so():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("choose-window")

            state.choose_window_filter.insert_text("no window is numbered 99")

        rows = state.layout_manager._choose_window_tokens()
        assert len(rows) == 1
        assert "No window matches." in rows[0][1]


@in_a_loop
async def test_escape_leaves_the_search_and_keeps_the_chooser():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

            fire(state, "/")
            state.choose_window_filter.insert_text("1")

        fire(state, "escape")

        assert state.choose_window_filter.text == ""
        assert state.choose_window
        # The keys of the chooser answer again, now the search is
        # not in the way.
        fire(state, "down")
        assert state.choose_window_index == 1


@in_a_loop
async def test_the_template_runs_on_the_chosen_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window \"select-window -t ':%%'\"")

        # The chooser opens on the window the client looks at: the
        # last one made. Choosing the first row runs the template
        # with that window's target.
        for _ in pymux.arrangement.windows:
            fire(state, "up")
        fire(state, "enter")

        assert not state.choose_window
        with set_app(state.app):
            assert (
                pymux.arrangement.get_active_window()
                is pymux.arrangement.windows[0]
            )


@in_a_loop
async def test_a_click_on_a_row_chooses_its_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("new-window")
            pymux.handle_command("choose-window")

        tokens = state.layout_manager._choose_window_tokens()
        handler = tokens[0][2]
        assert callable(handler)

        # The click reaches the handler with the client's app
        # current, as the renderer's call does.
        with set_app(state.app):
            assert handler(_Click()) is None

        assert not state.choose_window
        with set_app(state.app):
            assert (
                pymux.arrangement.get_active_window()
                is pymux.arrangement.windows[0]
            )


@in_a_loop
async def test_a_window_of_more_than_one_pane_says_how_many():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("split-window")
            pymux.handle_command("choose-window")

        rows = state.layout_manager._choose_window_tokens()
        assert "2 panes" in rows[-1][1]
        assert not any("1 panes" in text for _, text, *_ in rows)
