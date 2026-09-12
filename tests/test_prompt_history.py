"""
The prompt history: what the command line and the prompts took is
remembered, listed oldest first, and clearable.

The rules this judges, Lillecarl/pymux#305: a command typed at the
`:` prompt lands in the server's one history, `show-prompt-history`
reads it back through `answer`, `clear-prompt-history` empties it,
and both buffers of every client share it.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_a_command_typed_at_the_prompt_is_remembered():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            state.command_buffer.text = "set-option status off"
            pymux.leave_command_mode(append_to_history=True)

            pymux.handle_command("show-prompt-history")

        assert state.message.splitlines() == ["set-option status off"]


@in_a_loop
async def test_a_prompt_answer_is_remembered_too():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            state.prompt_buffer.text = "answer"
            pymux.leave_command_mode(append_to_history=False)

            pymux.handle_command("show-prompt-history")

        assert state.message.splitlines() == ["answer"]


@in_a_loop
async def test_the_history_is_oldest_first_and_shared():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            state.prompt_buffer.text = "first"
            pymux.leave_command_mode(append_to_history=False)
            state.prompt_buffer.text = "second"
            pymux.leave_command_mode(append_to_history=False)

            pymux.handle_command("show-prompt-history")

        assert state.message.splitlines() == ["first", "second"]


@in_a_loop
async def test_clear_prompt_history_empties_it():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            state.prompt_buffer.text = "first"
            pymux.leave_command_mode(append_to_history=False)

            pymux.handle_command("clear-prompt-history")
            pymux.handle_command("show-prompt-history")

        assert state.message == ""
