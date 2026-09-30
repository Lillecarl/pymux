"""
`customize-mode`: the options in a box, and a prompt to change one
from.

The rows are the options with what they hold; the search narrows
them; taking a row opens the command prompt with the option named
and what it holds as the default answer. Lillecarl/pymux#297.
"""

from prompt_toolkit.application.current import set_app

from session import create_session


async def test_options_list_with_their_values():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")

            assert state.choose_options
            rows = state.layout_manager._choose_options_tokens()
            text = "".join(t for _s, t, *_ in rows)
            assert "status-interval" in text
            assert "mouse" in text


async def test_search_narrows_options():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("status-int")

        rows = state.layout_manager._choose_options_tokens()
        text = "".join(t for _s, t, *_ in rows)
        assert "status-interval" in text
        assert "mouse" not in text


async def test_taking_row_asks_for_value():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")

            state.choose_window_index = 0
            state.layout_manager.choose_pointed_option()

        assert not state.choose_options
        assert state.prompt_command.startswith("set-option ")
        assert state.prompt_command.endswith(" %")


async def test_value_shown_is_what_option_holds():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status-interval 5")
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("status-interval")

        rows = state.layout_manager._choose_options_tokens()
        text = "".join(t for _s, t, *_ in rows)
        assert "5" in text


async def test_a_client_option_is_listed():
    """
    A client option is one of the three scopes, and the chooser is
    where a person browses what pymux can do. Lillecarl/pymux#472.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")

            rows = state.layout_manager._choose_options_tokens()
            text = "".join(t for _s, t, *_ in rows)
            assert "theme" in text
            assert "full-screen" in text
            assert " (client)" in text


async def test_a_client_option_is_set_by_its_own_command():
    """
    Taking a client row asks on the prompt with `set-client-option`,
    not `set-option`, which refuses the name. Lillecarl/pymux#472.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("theme")

            state.choose_window_index = 0
            state.layout_manager.choose_pointed_option()

        assert state.prompt_command.startswith("set-client-option theme ")
        assert state.prompt_command.endswith(" %")


async def test_taking_a_client_row_changes_this_client():
    """
    The whole path: the row, the prompt's command, and the set. A
    client option has to reach the client the chooser is drawn on, so
    the command it builds runs against nobody else.
    Lillecarl/pymux#472.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("full-screen")

            state.choose_window_index = 0
            state.layout_manager.choose_pointed_option()
            pymux.handle_command(state.prompt_command.replace("%", "on"))

            assert pymux.the_client_to_tell().full_screen is True


async def test_a_window_option_is_set_by_its_own_command():
    """
    The chooser listed a window option but set it with `set-option`,
    which takes only the session's, so the row was a name that errored
    when a person took it. Lillecarl/pymux#472.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("window-size")

            state.choose_window_index = 0
            state.layout_manager.choose_pointed_option()

        assert state.prompt_command.startswith("set-window-option window-size ")
        assert state.prompt_command.endswith(" %")
