"""
`show-options` and `show-window-options`: the read side of the
option commands, with the wording of `set-option` without a value.

The rules this judges, Lillecarl/pymux#298: a name says what that
one holds, without one the scope lists itself one `name value` line
per option sorted, the two commands keep their kinds apart
(`window_option` on the option says which), and `-g` on a window
option reads what every new window starts with.
"""

import pytest
from prompt_toolkit.application.current import set_app

from session import create_session, in_a_loop
from pymux.commands.commands import CommandException, show_options, show_window_options
from pymux.options import ALL_OPTIONS


@in_a_loop
async def test_a_session_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status off")
            pymux.handle_command("show-options status")

        assert state.message == "off"


@in_a_loop
async def test_the_session_list_holds_the_session_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "status" in names
        assert "strip" not in names  # a window option
        assert names == {name for name, o in ALL_OPTIONS.items() if not o.window_option}


@in_a_loop
async def test_a_window_option_reads_the_active_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g strip on")
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "on"


@in_a_loop
async def test_the_window_list_holds_the_window_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "strip" in names
        assert "status" not in names  # a session option


@in_a_loop
async def test_a_default_nobody_set_reads_as_not_set():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "not set"


@in_a_loop
async def test_the_wrong_kind_of_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, {"-g": False, "<option>": "strip"})

        with pytest.raises(CommandException):
            show_window_options(pymux, {"-g": False, "<option>": "status"})


@in_a_loop
async def test_an_unknown_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, {"-g": False, "<option>": "not-an-option"})


@in_a_loop
async def test_list_commands_lists_every_command_with_its_description():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("list-commands")

        rows = state.message.splitlines()
        names = [row.split()[0] for row in rows]
        assert names == sorted(names)
        assert "list-commands" in names
        assert "swap-window" in names
        # Each row carries the first line of the handler's docstring.
        row = [one for one in rows if one.split()[0] == "swap-window"][0]
        assert "Swap" in row


@in_a_loop
async def test_refresh_client_asks_its_own_app_for_a_frame():
    async with create_session() as (pymux, state):
        asked = []
        real = state.app.invalidate
        state.app.invalidate = lambda: (asked.append(True), real())[1]

        with set_app(state.app):
            pymux.handle_command("refresh-client")

        assert asked
