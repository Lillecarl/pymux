"""
A legacy program gets the answer of the terminal the key came from.

Where the terminals split on a key, pyte holds one `KeyProfile` for
each, and pymux picks the profile of the client that pressed it: by
its terminal's XTVERSION answer, then by its TERM, then by the
"key-profile" option. Lillecarl/pymux#706.
"""

from __future__ import annotations

import sys

import pytest
from prompt_toolkit.data_structures import Size
from pyte.keys.events import KEY_PROFILES
from pyte.sequences.replies import parse_reply
from session import once, over_connection

from pymux.core.main import Pymux
from pymux.options.options import ALL_OPTIONS, SetOptionError

SIZE = Size(rows=24, columns=80)
WAITS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)
SUPER_DELETE = "\x1b[3;9~"


def _what_the_pane_reads(pymux) -> list:
    written: list = []
    pymux.arrangement.get_active_window().active_pane.process.write_input = written.append
    return written


async def _super_delete_from(version: str | None, fallback: str | None = None) -> str:
    "What a legacy pane reads for super+Delete from a client whose terminal answered `version`."
    async with over_connection() as session:
        if fallback is not None:
            ALL_OPTIONS["key-profile"].set_value(session.pymux, fallback)
        await session.pymux.panes.create_window(WAITS)
        state, _ = await session.attach("one", SIZE)
        if version is not None:
            state.connection.graphics.handle_reply(parse_reply("\x1bP>|%s\x1b\\" % version))
        written = _what_the_pane_reads(session.pymux)
        session.typed(state, SUPER_DELETE)
        await once(lambda: written, 5.0, "the key never reached the pane")
        return "".join(written)


async def test_a_client_in_kitty_sends_what_kitty_sends():
    assert await _super_delete_from("kitty(0.49.2)") == "\x1b[3;9~"


async def test_a_client_in_wezterm_sends_what_wezterm_sends():
    assert await _super_delete_from("WezTerm 20240203-110809-5046fc22") == "\x1b[3~"


async def test_f13_from_wezterm_reaches_a_legacy_pane_as_rxvt_writes_it():
    "A key prompt_toolkit has no name for still reaches the pane. Lillecarl/pymux#707."
    async with over_connection() as session:
        await session.pymux.panes.create_window(WAITS)
        state, _ = await session.attach("one", SIZE)
        state.connection.graphics.handle_reply(parse_reply("\x1bP>|WezTerm 20240203-110809-5046fc22\x1b\\"))
        written = _what_the_pane_reads(session.pymux)
        session.typed(state, "\x1b[57376u")
        await once(lambda: written, 5.0, "F13 never reached the pane")
        assert "".join(written) == "\x1b[25~"


async def test_a_client_whose_terminal_named_nothing_gets_the_option():
    assert await _super_delete_from(None, fallback="xterm") == "\x1b[3~"
    assert await _super_delete_from(None) == "\x1b[3;9~"


async def test_the_key_profile_is_the_identity_of_the_terminal(monkeypatch):
    """
    One place names a client's terminal. A client that reports no TERM,
    as an integrated server's own does, is named by the server's TERM,
    and the key profile reads that same name. Lillecarl/pymux#841.
    """
    monkeypatch.setenv("TERM", "wezterm")
    async with over_connection() as session:
        await session.pymux.panes.create_window(WAITS)
        state, _ = await session.attach("one", SIZE)
        state.connection.colors.term = ""
        identity = state.connection.replies.identity
        assert identity.key_profile == "wezterm"
        assert session.pymux.key_profile_of(state) is KEY_PROFILES["wezterm"]


def test_kitty_answers_for_nobody_by_default():
    assert Pymux().key_profile_of(None) is KEY_PROFILES["kitty"]


def test_the_option_takes_only_a_profile():
    with pytest.raises(SetOptionError):
        ALL_OPTIONS["key-profile"].set_value(Pymux(), "foot")
