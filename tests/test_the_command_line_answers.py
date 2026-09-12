"""
The command line gets answers.

A command that runs from the command line runs on a client that draws
nothing, and the answers it asks for -- a listing, a value, an id --
have to come back on stdout. They used to go to a popup on that
client, which went nowhere. Lillecarl/pymux#288, #289, #292.
"""

from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop


@in_a_loop
async def test_list_windows_answers_the_command_line():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("list-windows")

        assert pymux.command_output, "the command line heard nothing"


@in_a_loop
async def test_list_panes_answers_the_command_line():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("list-panes")

        assert pymux.command_output, "the command line heard nothing"


@in_a_loop
async def test_a_listing_shows_in_a_pane_and_prints_on_the_command_line():
    """
    The two ways one listing reaches a person: a popup for a person in
    a pane, stdout for the command line.
    """
    async with create_session() as (pymux, state):
        shown = []
        state.layout_manager.display_popup = lambda title, text: shown.append(
            (title, text)
        )

        with set_app(state.app):
            pymux.handle_command("list-windows")
        assert shown and shown[0][0] == "list-windows"
        assert pymux.command_output is None

        pymux.command_output = []
        pymux.handle_command("list-windows")
        assert pymux.command_output
        assert len(shown) == 1, "the command line drew a popup"


@in_a_loop
async def test_display_message_p_prints_the_answer():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("display-message -p hello")

        assert pymux.command_output == ["hello"]


@in_a_loop
async def test_display_message_without_p_shows_the_message():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("display-message hello")

        assert state.message == "hello"
        assert pymux.command_output is None


@in_a_loop
async def test_get_paneid_names_the_pane():
    async with create_session() as (pymux, state):
        pane = pymux.arrangement.get_active_pane()

        pymux.command_output = []
        pymux.handle_command("get paneid")
        assert pymux.command_output == [str(pane.pane_id)]

        pymux.command_output = []
        pymux.handle_command("get -t %%%i paneid" % pane.pane_id)
        assert pymux.command_output == [str(pane.pane_id)]


@in_a_loop
async def test_set_option_without_a_value_answers_what_it_holds():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("set-option status")
        assert pymux.command_output == ["status on"]

        pymux.handle_command("set-option status off")
        pymux.command_output = []
        pymux.handle_command("set-option status")
        assert pymux.command_output == ["status off"]


@in_a_loop
async def test_set_window_option_g_answers_the_default():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("set-window-option -g strip on")
        pymux.handle_command("set-window-option -g strip")

        assert pymux.command_output == ["strip on"]


@in_a_loop
async def test_an_option_that_was_never_set_says_so():
    async with create_session() as (pymux, state):
        pymux.command_output = []

        pymux.handle_command("set-window-option -g strip")

        assert pymux.command_output == ["strip not set"]
