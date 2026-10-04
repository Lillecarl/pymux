"""
`info`: the whole server as JSON, for an agent to orient itself.

`caller` is the window and pane the command arrived from; `clients`
is what every attached person looks at; `sessions` holds every
window and pane, with the directory each reported, the variables
each published and the command each runs.
"""

import contextlib
import json

from contextlib import asynccontextmanager
from types import SimpleNamespace

from session import create_session
from prompt_toolkit.application.current import set_app


@contextlib.contextmanager
def calling_as(pymux, session, caller_pane_id=None):
    """
    Answer `get_client_state` with a socket command from a pane, and
    hand the fake back: answers go to its message line, the way they
    go to the stdout of a real socket caller.
    """
    real = pymux.get_client_state
    fake = SimpleNamespace(
        temporary=True, caller_pane_id=caller_pane_id, session=session
    )
    pymux.get_client_state = lambda: fake
    try:
        yield fake
    finally:
        pymux.get_client_state = real


@asynccontextmanager
async def reported_session():
    """
    A session of three windows: the plain one, one split in two, one
    alone. The non-active pane of the split window reported
    directory, variables and an exit status.
    """
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("split-window 'sleep 30'")
            split = pymux.arrangement.get_active_window()
            active = pymux.arrangement.get_active_pane()
            reported = next(pane for pane in split.panes if pane is not active)
            reported.current_directory = "/home/you"
            reported.current_host = "host"
            reported.user_vars["BRANCH"] = "main"
            reported.last_exit_status = 3

            pymux.handle_command("new-window 'sleep 30'")

            pymux.handle_command("info")

        yield pymux, state, json.loads(state.message)


def reported_pane(tree):
    "The pane that reported, wherever the windows hold it."
    for window in tree["sessions"][0]["windows"]:
        for pane in window["panes"]:
            if pane["current_directory"] == "/home/you":
                return window, pane
    raise AssertionError("no reported pane in tree")


async def test_tree_lists_every_window_and_pane():
    async with reported_session() as (pymux, _state, tree):
        (session,) = tree["sessions"]
        mine = session["windows"][-2:]
        assert [mine[1]["index"] - mine[0]["index"]] == [1]
        assert [len(window["panes"]) for window in mine] == [2, 1]
        assert [window["active"] for window in mine] == [False, True]

        panes = mine[0]["panes"]
        assert sorted(pane["active"] for pane in panes) == [False, True]
        assert all(pane["width"] > 0 and pane["height"] > 0 for pane in panes)


async def test_reported_state_travels_in_tree():
    async with reported_session() as (_pymux, _state, tree):
        window, reported = reported_pane(tree)
        assert reported["current_directory"] == "/home/you"
        assert reported["current_host"] == "host"
        assert reported["user_vars"] == {"BRANCH": "main"}
        assert reported["last_exit_status"] == 3

        other = next(pane for pane in window["panes"] if pane is not reported)
        assert other["current_directory"] is None
        assert other["user_vars"] == {}
        assert other["last_exit_status"] is None


async def test_real_client_has_no_caller_but_is_listed():
    async with reported_session() as (pymux, state, tree):
        assert tree["caller"] is None

        (client,) = tree["clients"]
        with set_app(state.app):
            active = pymux.arrangement.get_active_pane()
        assert client["pane_id"] == active.pane_id
        window = next(
            window
            for window in tree["sessions"][0]["windows"]
            if any(pane["pane_id"] == active.pane_id for pane in window["panes"])
        )
        assert client["window_id"] == window["window_id"]


async def test_calling_pane_names_caller():
    async with reported_session() as (pymux, state, tree):
        window, reported = reported_pane(tree)
        caller_id = reported["pane_id"]
        with set_app(state.app), calling_as(
            pymux, pymux.current_session, caller_pane_id=caller_id
        ) as caller_state:
            pymux.handle_command("info")

        caller = json.loads(caller_state.message)["caller"]
        assert caller["pane_id"] == caller_id
        assert caller["window_id"] == window["window_id"]


async def test_dead_caller_names_nobody():
    async with reported_session() as (pymux, state, _tree):
        with set_app(state.app), calling_as(
            pymux, pymux.current_session, caller_pane_id=999999
        ) as caller_state:
            pymux.handle_command("info")

        assert json.loads(caller_state.message)["caller"] is None
