"""
`customize-mode`: the options in a box, and a prompt to change one
from.

The rows are the options with what they hold; the search narrows
them; taking a row opens the command prompt with the option named
and what it holds as the default answer. Lillecarl/pymux#297.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_the_options_list_with_their_values():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")

            assert state.choose_options
            rows = state.layout_manager._choose_options_tokens()
            text = "".join(t for _s, t, *_ in rows)
            assert "status-interval" in text
            assert "mouse" in text


@in_a_loop
async def test_the_search_narrows_the_options():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("status-int")

        rows = state.layout_manager._choose_options_tokens()
        text = "".join(t for _s, t, *_ in rows)
        assert "status-interval" in text
        assert "mouse" not in text


@in_a_loop
async def test_taking_a_row_asks_for_a_value():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("customize-mode")

            state.choose_window_index = 0
            state.layout_manager.choose_the_pointed_option()

        assert not state.choose_options
        assert state.prompt_command.startswith("set-option ")
        assert state.prompt_command.endswith(" %")


@in_a_loop
async def test_the_value_shown_is_what_the_option_holds():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status-interval 5")
            pymux.handle_command("customize-mode")
            state.choose_window_filter.insert_text("status-interval")

        rows = state.layout_manager._choose_options_tokens()
        text = "".join(t for _s, t, *_ in rows)
        assert "5" in text
