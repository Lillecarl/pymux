"""
A command typed in a pane talks to the person, not to a fake CLI.

A command that arrives over a socket runs under a temporary client
state: it draws nothing, and it is taken away as soon as the command is
answered. Anything a command puts there is lost without a word.

Measured before the fix, driving the real connection route:

    display-message hello                    message=None
    confirm-before -p '...' '...'            confirm=None
    list-panes                               answered on stdout

So a listing was already right -- `show_listing` writes to stdout when a
command came over the command line (Lillecarl/pymux#288) -- and the two
that talk to `get_client_state()` directly were not.
Lillecarl/pymux#272.
"""

from prompt_toolkit.data_structures import Size

from session import once, over_connection

SIZE = Size(rows=24, columns=80)


async def test_display_message_from_a_pane_reaches_the_person():
    with over_connection() as session:
        state, _ = await session.attach("only", SIZE)

        await session.command("display-message hello")

        await once(
            lambda: state.message == "hello",
            5.0,
            "the message never reached the attached client",
        )


async def test_confirm_before_from_a_pane_asks_the_person():
    with over_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        await session.command(
            "confirm-before -p 'Really? (y/n)' 'display-message kept'"
        )

        await once(
            lambda: state.confirm_text == "Really? (y/n)",
            5.0,
            "the question never reached the attached client",
        )

        # And it is answerable, which is the whole point: the command
        # behind a question nobody can see never runs.
        session.typed(state, "y")
        await once(
            lambda: state.message == "kept", 5.0, "the yes never ran the command"
        )
        assert pymux is not None


async def test_a_listing_from_a_pane_still_answers_on_stdout():
    """
    The other half, so a change to one is not made at the cost of the
    other. A listing goes to the person who typed the command, which is
    stdout, and not to a popup on somebody else's screen.
    """
    with over_connection() as session:
        await session.attach("only", SIZE)

        got = await session.command("list-panes")

        await once(
            lambda: any(b'"out"' in packet for packet in got),
            5.0,
            "the listing never came back on the connection",
        )
