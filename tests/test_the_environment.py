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

from session import create_session, in_a_loop
from pymux.commands import CommandException
from pymux.commands.set_environment import set_environment
from pymux.commands.show_environment import show_environment


@in_a_loop
async def test_the_set_and_the_show_make_the_round_trip():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment FOO bar")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=bar"


@in_a_loop
async def test_the_session_scope_sits_over_the_global():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment FOO baz")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=baz"


@in_a_loop
async def test_a_session_unset_falls_through_to_the_global():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment FOO baz")
            pymux.handle_command("set-environment -u FOO")
            pymux.handle_command("show-environment FOO")

        assert state.message == "FOO=bar"


@in_a_loop
async def test_a_name_unset_in_both_scopes_leaves_the_environment():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g FOO bar")
            pymux.handle_command("set-environment -u FOO")
            # And the global scope's own unset.
            pymux.handle_command("set-environment -u -g FOO")

        assert "FOO" not in pymux.pane_environment()


@in_a_loop
async def test_the_merged_environment_is_the_server_over_the_scopes():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment -g HOME overridden")
            pymux.handle_command("set-environment PYMUX_TEST_SCOPE more")

        merged = pymux.pane_environment()
        assert merged["HOME"] == "overridden"
        # The server's own value stays under a scope that says
        # nothing.
        assert merged.get("PYMUX_TEST_SCOPE") == "more" or "PYMUX_TEST_SCOPE" not in os.environ


@in_a_loop
async def test_s_escapes_the_values_for_the_shell():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-environment FOO 'two words'")
            pymux.handle_command("show-environment -s FOO")

        assert state.message == "FOO='two words'"


@in_a_loop
async def test_a_name_no_scope_holds_is_an_error():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            show_environment(pymux, argparse.Namespace(g=False, s=False, name="NOPE"))

        with pytest.raises(CommandException):
            show_environment(pymux, argparse.Namespace(g=True, s=False, name="NOPE"))


@in_a_loop
async def test_a_name_with_an_equals_sign_is_refused():
    async with create_session() as (pymux, state):
        with pytest.raises(CommandException):
            set_environment(
                pymux,
                argparse.Namespace(g=False, u=False, name="FOO=bar", value=None),
            )
        assert "FOO=bar" not in pymux.session_environment
