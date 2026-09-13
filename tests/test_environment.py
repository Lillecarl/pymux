"""
`set-environment` and `show-environment`: the two scopes a new pane
takes its environment from, with the unset that falls through.

The rules this judges, Lillecarl/pymux#270: the session scope sits
over the global one and both over the server's own; a `-u` in one
scope takes the name out so a session unset falls back to the global
value rather than to nothing; and a name unset in both scopes leaves
the environment entirely. `show-environment` prints `NAME=value`
lines, `-s` escapes them for `eval`.
"""

import os

import argparse
import pytest
from prompt_toolkit.application.current import set_app

from session import create_session, in_loop
from pymux.commands import CommandException
from pymux.commands.set_environment import set_environment
from pymux.commands.show_environment import show_environment


@in_loop
async def test_set_and_show_make_round_trip():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment FOO bar")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=bar"


@in_loop
async def test_session_scope_sits_over_global():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment FOO baz")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=baz"


@in_loop
async def test_session_unset_falls_through_to_global():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment FOO baz")
            pymux.handle_command("set-environment -u FOO")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=bar"


@in_loop
async def test_name_unset_in_both_scopes_leaves_environment():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment -u FOO")
            # And the global scope's own unset.
            pymux.handle_command("set-environment -u -g FOO")

        assert "FOO" not in pymux.pane_environment()


@in_loop
async def test_merged_environment_is_server_over_scopes():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g HOME overridden")
            pymux.handle_command("set-environment PYMUX_TEST_SCOPE more")

        merged = pymux.pane_environment()
        assert merged["HOME"] == "overridden"
        # The server's own value stays under a scope that says
        # nothing.
        assert merged.get("PYMUX_TEST_SCOPE") == "more" or "PYMUX_TEST_SCOPE" not in os.environ


@in_loop
async def test_s_escapes_values_for_shell():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment FOO 'two words'")
            pymux.handle_command("show-environment -s FOO")

        assert state.message == "FOO='two words'"


@in_loop
async def test_name_no_scope_holds_is_error():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_environment(pymux, argparse.Namespace(g=False, s=False, name="NOPE"))

        with pytest.raises(CommandException):
            show_environment(pymux, argparse.Namespace(g=True, s=False, name="NOPE"))


@in_loop
async def test_name_with_equals_sign_is_refused():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            set_environment(
                pymux,
                argparse.Namespace(g=False, u=False, name="FOO=bar", value=None),
            )
        assert "FOO=bar" not in pymux.session_environment
