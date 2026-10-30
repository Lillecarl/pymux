"""
Tab completion of the things a command targets: sessions, windows,
panes, clients and jobs, found by their spelling or by what they say
about themselves.
"""

from __future__ import annotations

import re
import sys

import pytest
from prompt_toolkit.document import Document
from run_commands import over_socket

from pymux.commands import parser_tree
from pymux.commands.completer import TARGET_COMPLETERS, create_command_completer
from pymux.core.main import Pymux

ENDS_AT_ONCE = "%s -c pass" % (sys.executable,)


@pytest.fixture
async def pymux():
    mux = Pymux()
    async with mux.running():
        yield mux


async def two_sessions(mux):
    "`home` with two windows, the second called `editor`, and `work` with one."
    await over_socket(mux, "new-session", "-d", "-s", "home", ENDS_AT_ONCE)
    home = mux.get_session("home")
    await mux.panes.create_window(ENDS_AT_ONCE, name="editor", session=home)
    await over_socket(mux, "new-session", "-d", "-s", "work", ENDS_AT_ONCE)


def offered(pymux, line) -> dict[str, str]:
    "What the command bar offers for a line, word to what it says."
    completer = create_command_completer(pymux)
    out = {}
    for c in completer.get_completions(Document(line, len(line)), None):
        meta = c.display_meta
        out[c.text] = "".join(text for _style, text, *_ in meta) if meta else ""
    return out


def test_every_target_a_command_spells_completes():
    "A new command that names a target the tmux way completes it for free, or fails here."
    _parser, subparsers = parser_tree()
    spelled = re.compile(r"<(target|src|dst)-(session|window|pane|client)>|<job>")
    missing = {
        (name, action.metavar)
        for name, parser in subparsers.choices.items()
        for action in parser._actions
        if isinstance(action.metavar, str) and spelled.search(action.metavar)
        if action.metavar not in TARGET_COMPLETERS
    }
    assert missing == set()


async def test_a_session_target_offers_every_session(pymux):
    await two_sessions(pymux)

    found = offered(pymux, "switch-client -t ")

    assert {"home", "work"} <= set(found)
    assert "2 windows" in found["home"]


async def test_a_window_is_found_by_its_name(pymux):
    await two_sessions(pymux)
    editor = pymux.get_session("home").arrangement.windows[1]

    found = offered(pymux, "select-window -t edit")

    assert list(found) == ["home:%s" % editor.index]


async def test_a_pane_is_offered_by_its_id_and_its_place(pymux):
    await two_sessions(pymux)
    pane = pymux.get_session("work").arrangement.windows[0].panes[0]

    found = offered(pymux, "select-pane -t ")

    assert found["%%%s" % pane.pane_id].startswith("work:")


async def test_a_saved_question_replaces_what_tab_offers(pymux):
    "The values are what the question of that name answers, so saving another changes them."
    await two_sessions(pymux)
    pymux.jobs.save_query("complete-session", "SELECT upper(session_name), 'shouted' FROM live.sessions", None)

    found = offered(pymux, "switch-client -t ")

    assert {"HOME", "WORK"} <= set(found)
    assert set(found.values()) == {"shouted"}


async def test_move_window_completes_where_a_window_can_go(pymux):
    await two_sessions(pymux)

    assert {"home:", "work:"} <= set(offered(pymux, "move-window -t "))
    assert {"home", "work"} <= set(offered(pymux, "move-window --session "))
