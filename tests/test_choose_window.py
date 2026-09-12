"""
`choose-window`: prefix w opens a chooser over the windows of the
session, and Enter switches to the window the chooser points at.

The rules this judges, Lillecarl/pymux#295: it opens on the window
this client looks at, the keys move the point and clamp at the ends,
Enter switches and closes, and q and Escape close without switching.
"""

from prompt_toolkit.application.current import set_app
from prompt_toolkit.keys import Keys

from session import create_session, in_a_loop

#: prompt_toolkit keeps the keys it can name as `Keys` members and
#: the printable ones as themselves.
_KEYS = {"up": Keys.Up, "down": Keys.Down, "enter": Keys.Enter, "escape": Keys.Escape}


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
        assert (
            pymux.arrangement.get_active_window() is pymux.arrangement.windows[-1]
        )

        with set_app(state.app):
            pymux.handle_command("choose-window")
        fire(state, "escape")
        assert not state.choose_window
        assert (
            pymux.arrangement.get_active_window() is pymux.arrangement.windows[-1]
        )


@in_a_loop
async def test_the_prefix_w_binding_opens_it():
    "The tmux key: prefix w opens the chooser, as it opens the tree."
    async with create_session() as (pymux, state):
        assert ("w", "choose-window") in (
            pymux.key_bindings_manager.keys_a_prefix_leads_to()
        )
