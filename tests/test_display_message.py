"""
`display-message` says something on the status line.

**The message is a format.** It was drawn as it was typed, so
`display-message '#{session_name}'` answered with the format string
rather than the name, while `display-message -p` beside it formatted
properly. A binding that asks the session something is the whole use of
the command, and tmux expands it either way. Lillecarl/pymux#334.
"""

from prompt_toolkit.application.current import set_app

from session import create_session


async def _run(pymux, state, command) -> None:
    with set_app(state.app):
        pymux.handle_command(command)


async def test_a_message_is_a_format():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "display-message '#{session_name}'")

        assert state.message == pymux.session_name


async def test_a_message_may_be_a_template():
    "The other language reaches here too. Lillecarl/pymux#333."
    async with create_session() as (pymux, state):
        await _run(
            pymux, state, "display-message '{{ session_name | upper }}'"
        )

        assert state.message == pymux.session_name.upper()


async def test_a_message_with_nothing_in_it_is_itself():
    async with create_session() as (pymux, state):
        await _run(pymux, state, "display-message 'nothing to expand'")

        assert state.message == "nothing to expand"


async def test_a_message_reads_the_client_it_is_shown_to():
    """
    The client is what a `client_` variable reads, and the session is
    that client's. A message drawn for somebody else would answer for
    whoever happened to ask. Lillecarl/pymux#323.
    """
    async with create_session() as (pymux, state):
        state.connection.hostname = "buildbox-3"

        await _run(pymux, state, "display-message 'from #{client_hostname}'")

        assert state.message == "from buildbox-3"


async def test_printing_a_message_still_answers_on_the_command_line():
    "`-p` is the other half, and it formatted all along."
    async with create_session() as (pymux, state):
        pymux.command_output = []
        try:
            await _run(pymux, state, "display-message -p '#{session_name}'")
            said = list(pymux.command_output)
        finally:
            pymux.command_output = None

        assert said == [pymux.session_name]
        assert state.message is None, "a printed message was also shown"
