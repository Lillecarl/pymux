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

from __future__ import annotations

import argparse

import pytest
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.commands import CommandException
from pymux.commands.show_client_options import show_client_options
from pymux.commands.show_options import show_options
from pymux.commands.show_window_options import show_window_options
from pymux.options import (
    ALL_CLIENT_OPTIONS,
    ALL_OPTIONS,
    ALL_WINDOW_OPTIONS,
    KeyPrefixOption,
    Scope,
)


async def test_session_option_reads_as_it_is_written():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option status off")
            pymux.handle_command("show-options status")

        assert state.message == "off"


async def test_mode_keys_reads_the_word_tmux_reads():
    """
    The mode is a bool inside and a word outside. tmux answers `vi`
    and `emacs`; `on` answers nothing, and the completion offers the
    words the read-back would not say. Lillecarl/pymux#382.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options mode-keys")

            assert state.message == "emacs"

            pymux.handle_command("set-option mode-keys vi")
            pymux.handle_command("show-options mode-keys")

            assert state.message == "vi"


async def test_session_list_holds_session_options_only():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("show-options")

        rows = state.message.splitlines()
        names = {row.split()[0] for row in rows}
        assert "status" in names
        assert "strip" not in names  # a window option
        assert names == {name for name, o in ALL_OPTIONS.items() if o.scope is Scope.SESSION}


async def test_window_option_reads_active_window():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g strip on")
            pymux.handle_command("show-window-options -g strip")

        assert state.message == "on"


async def test_base_index_reads_as_it_is_written():
    "It is set and working, so it must not read as not set. Lillecarl/pymux#494."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option base-index 0")
            pymux.handle_command("show-options base-index")

        assert state.message == "0"


async def test_window_size_default_reads_as_it_is_written():
    "Lillecarl/pymux#494."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option -g window-size latest")
            pymux.handle_command("show-window-options -g window-size")

        assert state.message == "latest"


async def test_window_size_reads_active_window():
    "Lillecarl/pymux#494."
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-window-option window-size largest")
            pymux.handle_command("show-window-options window-size")

        assert state.message == "largest"


def test_every_option_names_where_it_lives():
    """
    An option that keeps its value in an attribute names it, or the
    read side answers "not set" for a value that is set. The prefix
    key is the one deliberate exception: it lives in the binding
    manager. Lillecarl/pymux#494.
    """
    tables = (ALL_OPTIONS, ALL_WINDOW_OPTIONS, ALL_CLIENT_OPTIONS)
    for table in tables:
        for name, option in table.items():
            assert option.attribute_name is not None or isinstance(option, KeyPrefixOption), name


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
            show_client_options(pymux, argparse.Namespace(target_client=None, option="status"))

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
        row = next(one for one in rows if one.split()[0] == "swap-window")
        assert "Swap" in row


async def test_refresh_client_asks_its_own_app_for_frame():
    async with create_session() as (pymux, state):
        asked = []
        real = state.app.invalidate
        state.app.invalidate = lambda: (asked.append(True), real())[1]

        with set_app(state.app):
            pymux.handle_command("refresh-client")

        assert asked
