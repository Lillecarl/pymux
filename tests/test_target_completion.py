"""
Tab completion of the things a command targets: sessions, windows,
panes, clients and jobs, found by their spelling or by what they say
about themselves.
"""

from __future__ import annotations

import sys

import pytest
from prompt_toolkit.document import Document
from run_commands import over_socket

from pymux.commands import (
    CommandParser,
    Role,
    Target,
    add_command,
    add_target,
    parser_tree,
    refuse_undeclared_targets,
    target_of,
    targets_of,
)
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


def test_every_kind_of_target_completes():
    "A command that declares a target with `add_target` completes it for free."
    assert set(TARGET_COMPLETERS) == set(Target)


def test_a_target_spelled_with_a_plain_argument_fails_the_tree():
    "It would complete as nothing and reach another server as typed. Lillecarl/pyterm#965."
    subparsers = CommandParser(add_help=False).add_subparsers(parser_class=CommandParser)
    parser = add_command(subparsers, test_a_target_spelled_with_a_plain_argument_fails_the_tree, name="moves")
    add_target(parser, "-s", kind=Target.WINDOW, role=Role.SRC)
    parser.add_argument("-S", "--session", dest="session", metavar="<target-session>")

    with pytest.raises(TypeError, match=r"moves -S/--session$"):
        refuse_undeclared_targets(subparsers.choices)


def test_the_tree_declares_every_target_it_spells():
    "Each metavar a target has is the one its kind gives, and a peer reads the same kind."
    _parser, subparsers = parser_tree()
    move_window = subparsers.choices["move-window"]
    assert targets_of(move_window) == {
        "src_window": Target.WINDOW,
        "dst_window": Target.SLOT,
        "session": Target.SESSION,
    }
    spelled = {action.dest: action.metavar for action in move_window._actions if target_of(action)}
    assert spelled == {
        "src_window": "<src-window>",
        "dst_window": "[<dst-session>:]<dst-index>",
        "session": "<target-session>",
    }


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
