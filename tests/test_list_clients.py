"""
`list-clients` says who is attached, and from where.

Nothing listed the clients of a server. The hostname each client
reports when it attaches (Lillecarl/pymux#287) had no reader, and
`detach-client -a` was a blanket loop with nothing to name. This is the
reader: one line per client, tmux's shape, with the machine in place of
the tty -- a pymux client can be on another machine, and a tty path of
a machine you are not on names nothing. Lillecarl/pymux#330.
"""

from prompt_toolkit.data_structures import Size

from session import OTHER_MACHINE, over_connection

SIZE = Size(rows=24, columns=80)


async def _lines(session, command) -> list:
    "What a command printed on the command line."
    session.pymux.command_output = []
    try:
        session.pymux.handle_command(command)
        return list(session.pymux.command_output)
    finally:
        session.pymux.command_output = None


async def test_a_client_is_listed_by_the_machine_it_is_on():
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _lines(session, "list-clients -F '#{client_hostname}'")

        assert said == [OTHER_MACHINE]


async def test_every_client_gets_a_line():
    async with over_connection() as session:
        await session.attach("here", SIZE, hostname="workstation")
        await session.attach("there", SIZE, hostname="buildbox-7")

        said = await _lines(session, "list-clients -F '#{client_hostname}'")

        assert sorted(said) == ["buildbox-7", "workstation"]


async def test_a_client_is_listed_with_its_size_and_terminal():
    "The default format, which is what a person reads."
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _lines(session, "list-clients -F '%s'" % (
            "#{client_width}x#{client_height} #{client_termname}",
        ))

        assert said == ["80x24 xterm-256color"]


async def test_the_session_of_each_client_is_its_own():
    "A client watches one session, and the line is about that one."
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _lines(session, "list-clients -F '#{client_session}'")

        assert said == [session.pymux.sessions[0].name]


async def test_a_target_session_narrows_the_listing():
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        pymux = session.pymux
        name = pymux.sessions[0].name

        listed = await _lines(session, "list-clients -t %s -F '#{client_hostname}'" % (name,))
        assert listed == [OTHER_MACHINE]

        pymux.command_output = []
        pymux.command_error = []
        try:
            pymux.handle_command("list-clients -t nowhere")
            errors = list(pymux.command_error)
        finally:
            pymux.command_output = None
            pymux.command_error = None

        assert any("nowhere" in line for line in errors), errors


async def test_a_command_of_its_own_is_not_a_client():
    """
    A command that arrives over a socket runs under a temporary client
    that draws nothing. Nobody is sitting at one, so it is not listed.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _lines(session, "list-clients -F '#{client_hostname}'")

        assert len(said) == 1, said


async def test_a_listing_takes_a_template_too():
    "`-J`, like every other listing. Lillecarl/pymux#333."
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _lines(
            session,
            "list-clients -J '{{ client_hostname }} is {{ client_width }} wide'",
        )

        assert said == ["%s is 80 wide" % (OTHER_MACHINE,)]
