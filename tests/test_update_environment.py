"""
`update-environment`: the names that follow a client. #271.

**The machine a person attaches from is the machine their panes should
talk to.** `DISPLAY`, `SSH_AUTH_SOCK` and `WAYLAND_DISPLAY` name things
that live on that machine, and a server outlives the client that
started it. Over ssh (Lillecarl/pymux#90) an old `DISPLAY` from the
server's own start is worse than none: a pane opens a window nobody is
looking at.

So a client reports its environment when it attaches, the session takes
the names the option lists, and nothing keeps the rest.
"""

from prompt_toolkit.data_structures import Size

from session import in_this_process, over_connection

SIZE = Size(rows=24, columns=80)

#: What a client on somebody's desktop carries.
AT_A_DESK = {
    "DISPLAY": ":0",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "EDITOR": "vim",
    "XDG_SESSION_TYPE": "wayland",
}


async def test_a_session_takes_the_names_the_option_lists():
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE, environment=AT_A_DESK)

        assert state.session.environment["DISPLAY"] == ":0"
        assert state.session.environment["SSH_AUTH_SOCK"].endswith("/ssh")


async def test_a_name_the_option_does_not_list_is_left_alone():
    "A client brings its whole environment; the option says what counts."
    async with in_this_process() as session:
        state, _ = await session.attach("only", SIZE, environment=AT_A_DESK)

        assert "EDITOR" not in state.session.environment


async def test_the_second_client_refreshes_what_the_first_one_set():
    "The point of it: the newest attach owns these names."
    async with in_this_process() as session:
        first, _ = await session.attach("first", SIZE, environment={"DISPLAY": ":0"})
        assert first.session.environment["DISPLAY"] == ":0"

        second, _ = await session.attach(
            "second", SIZE, environment={"DISPLAY": ":1"}
        )

        assert second.session.environment["DISPLAY"] == ":1"


async def test_a_name_the_client_does_not_carry_leaves_the_session():
    """
    Not left behind. A stale `DISPLAY` is worse than none, because a
    pane opens a window on a screen nobody is at. tmux clears it the
    same way (`environ_update`).
    """
    async with in_this_process() as session:
        state, _ = await session.attach("first", SIZE, environment={"DISPLAY": ":0"})
        assert state.session.environment["DISPLAY"] == ":0"

        await session.attach("second", SIZE, environment={"EDITOR": "vim"})

        assert "DISPLAY" not in state.session.environment


async def test_a_pattern_takes_a_family_of_names():
    "tmux matches each entry with `fnmatch`, and so does this."
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.update_environment = "XDG_*"

        state, _ = await session.attach(
            "only",
            SIZE,
            environment={"XDG_SESSION_TYPE": "wayland", "XDG_RUNTIME_DIR": "/run/1000"},
        )

        assert state.session.environment["XDG_SESSION_TYPE"] == "wayland"
        assert state.session.environment["XDG_RUNTIME_DIR"] == "/run/1000"


async def test_what_a_person_set_by_hand_is_not_touched():
    "`set-environment` writes the same dictionary, and other names stay."
    async with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE, environment=AT_A_DESK)
        state.session.environment["MY_OWN"] = "kept"

        await session.attach("second", SIZE, environment={"DISPLAY": ":1"})

        assert state.session.environment["MY_OWN"] == "kept"
        assert pymux.update_environment.startswith("DISPLAY ")


async def test_the_option_reads_back_as_it_was_written():
    async with in_this_process() as session:
        pymux = session.pymux
        pymux.command_output = []
        try:
            pymux.handle_command("set-option update-environment 'DISPLAY MY_OWN'")
            pymux.handle_command("show-options update-environment")
            said = list(pymux.command_output)
        finally:
            pymux.command_output = None

        assert said[-1] == "DISPLAY MY_OWN"


async def test_a_new_pane_runs_with_what_the_client_brought():
    "Which is the whole point: the pane talks to the person's machine."
    async with in_this_process() as session:
        pymux = session.pymux
        await session.attach("only", SIZE, environment=AT_A_DESK)

        assert pymux.pane_environment()["DISPLAY"] == ":0"


async def test_the_client_reports_it_over_the_wire():
    "The other route: the `start-gui` packet carries it."
    async with over_connection() as session:
        state, _ = await session.attach("the client", SIZE, environment=AT_A_DESK)

        assert state.connection.environment["EDITOR"] == "vim"
        assert state.session.environment["DISPLAY"] == ":0"
        assert "EDITOR" not in state.session.environment
