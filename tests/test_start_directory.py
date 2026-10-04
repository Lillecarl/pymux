"""
New panes start where the active pane says it is.

A shell reports its directory ("OSC 7", "CurrentDir") on every
prompt. A window or pane that opens without `-c` starts there: the
reported directory wins over what the process table says, which
covers a pane that never reported, and nothing covers the first pane
of a session. An explicit `-c` already won before this was asked.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from session import in_this_process, once

from pymux.main import Pymux

SIZE = Size(rows=24, columns=80)


def test_reported_directory_wins_over_process_table():
    pymux = Pymux()
    pane = SimpleNamespace(current_directory="/reported")
    process = SimpleNamespace(get_cwd=lambda: "/proc-says")
    window = SimpleNamespace(active_pane=pane, active_process=process)
    assert pymux._directory_to_start_in(window, None) == "/reported"


def test_process_table_covers_pane_that_never_reported():
    pymux = Pymux()
    pane = SimpleNamespace(current_directory=None)
    process = SimpleNamespace(get_cwd=lambda: "/proc-says")
    window = SimpleNamespace(active_pane=pane, active_process=process)
    assert pymux._directory_to_start_in(window, None) == "/proc-says"


def test_unknown_process_table_covers_nothing():
    pymux = Pymux()
    pane = SimpleNamespace(current_directory=None)
    process = SimpleNamespace(get_cwd=lambda: None)
    window = SimpleNamespace(active_pane=pane, active_process=process)
    assert pymux._directory_to_start_in(window, None) is None


def test_new_window_reads_session_active_pane():
    "A window opens with no window to inherit from, only a session."
    pymux = Pymux()
    pane = SimpleNamespace(current_directory="/reported")
    window = SimpleNamespace(active_pane=pane)
    session = SimpleNamespace(arrangement=SimpleNamespace(get_active_window=lambda: window))
    assert pymux._directory_to_start_in(None, session) == "/reported"


def test_first_pane_of_session_starts_nowhere():
    pymux = Pymux()
    session = SimpleNamespace(arrangement=SimpleNamespace(get_active_window=lambda: None))
    assert pymux._directory_to_start_in(None, session) is None


async def test_new_window_starts_in_reported_directory():
    """
    The whole hook, from the report through the fork to the program.

    The pane prints where it stands on one line; the reader below
    polls for it, the way the shim test polls for its verdict.
    """
    async with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        with set_app(state.app):
            window = pymux.current_session.arrangement.get_active_window()
            window.active_pane.current_directory = "/tmp"

            program = "%s -c 'import os, time; print(\"CWD=\" + os.getcwd()); time.sleep(30)'" % (sys.executable,)
            pymux.create_window(program)

        pane = pymux.current_session.arrangement.get_active_window().panes[0]

        def page_text():
            return pane.screen.page.text(0, 23)

        await once(lambda: "CWD=" in page_text(), 5.0, "the pane never printed its directory")
        assert "CWD=/tmp" in page_text()


async def test_explicit_directory_wins_over_report():
    async with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", SIZE)

        with set_app(state.app):
            window = pymux.current_session.arrangement.get_active_window()
            window.active_pane.current_directory = "/tmp"

            program = "%s -c 'import os, time; print(\"CWD=\" + os.getcwd()); time.sleep(30)'" % (sys.executable,)
            pymux.create_window(program, start_directory="/")

        pane = pymux.current_session.arrangement.get_active_window().panes[0]

        def page_text():
            return pane.screen.page.text(0, 23)

        await once(lambda: "CWD=" in page_text(), 5.0, "the pane never printed its directory")
        assert "CWD=/" in page_text()
