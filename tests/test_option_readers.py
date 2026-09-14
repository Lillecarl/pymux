"""
`show-options`, `show-window-options` and `show-client-options`: the
read side of the option commands, with the wording of `set-option`
without a value.

The rules this judges, Lillecarl/pymux#298: a name says what that
one holds, without one the scope lists itself one `name value` line
per option sorted, the three commands keep their scopes apart
(`Option.scope` says which), and `-g` on a window option reads what
every new window starts with.
"""

import argparse
import pytest
from prompt_toolkit.application.current import set_app

from session import create_session
from pymux.commands import CommandException
from pymux.commands.show_client_options import show_client_options
from pymux.commands.show_options import show_options
from pymux.commands.show_window_options import show_window_options
from pymux.options import ALL_CLIENT_OPTIONS, ALL_OPTIONS, ALL_WINDOW_OPTIONS, Scope


async def test_session_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status off")
            pymux.handle_command("show-options status")

        assert state.message == "off"


async def test_session_list_holds_session_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "status" in names
        assert "strip" not in names  # a window option
        assert names == {
            name for name, o in ALL_OPTIONS.items() if o.scope is Scope.SESSION
        }


async def test_window_option_reads_active_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g strip on")
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "on"


async def test_window_list_holds_window_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "strip" in names
        assert "status" not in names  # a session option


async def test_default_nobody_set_reads_as_not_set():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "not set"


async def test_client_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-client-option theme grey")
            pymux.handle_command("show-client-options theme")

        assert state.message == "grey"


async def test_client_list_holds_client_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-client-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert names == set(ALL_CLIENT_OPTIONS)
        assert "status" not in names  # a session option
        assert "strip" not in names  # a window option


async def test_the_three_scopes_hold_every_option_between_them():
    "A scope nobody lists is an option nobody can read back."
    tables = (ALL_OPTIONS, ALL_WINDOW_OPTIONS, ALL_CLIENT_OPTIONS)
    wanted = {Scope.SESSION: 0, Scope.WINDOW: 1, Scope.CLIENT: 2}

    for scope, which in wanted.items():
        for name, option in tables[which].items():
            assert option.scope is scope, name


async def test_wrong_kind_of_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="strip"))

        with pytest.raises(CommandException):
            show_window_options(pymux, argparse.Namespace(g=False, option="status"))

        with pytest.raises(CommandException):
            show_client_options(
                pymux, argparse.Namespace(target_client=None, option="status")
            )

        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="theme"))


async def test_unknown_option_is_unknown():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_options(pymux, argparse.Namespace(g=False, option="not-an-option"))


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


async def test_refresh_client_asks_its_own_app_for_frame():
    async with create_session() as (pymux, state):
        asked = []
        real = state.app.invalidate
        state.app.invalidate = lambda: (asked.append(True), real())[1]

        with set_app(state.app):
            pymux.handle_command("refresh-client")

        assert asked
