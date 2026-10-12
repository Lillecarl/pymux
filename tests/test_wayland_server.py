"""
The compositor a server holds for panes with no display, against real
wl-copy and wl-paste. Needs the `wayland` extra, so `checks.pymux-wayland`
runs this and `checks.pymux-unit` skips it. Lillecarl/pymux#383.
"""

from __future__ import annotations

import contextlib
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from clipboard_terminal import ClipboardTerminal
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from session import once, over_connection

pytest.importorskip("pysmithay")

from pymux.main import Pymux
from pymux.options import Clipboard
from pymux.server import CLIPBOARD_READ_SECONDS

SIZE = Size(rows=24, columns=80)


@pytest.fixture
async def mux(tmp_path):
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    async with pymux.running():
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        try:
            yield pymux
        finally:
            pymux.stop()


@asynccontextmanager
async def attached(tmp_path, terminal: ClipboardTerminal, mode: Clipboard):
    "A server under `set-clipboard <mode>`, with one client at `terminal`."
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    pymux.clipboard_mode = mode
    async with over_connection(pymux=pymux, read_packet=terminal) as session:
        state, _size = await session.attach("one", SIZE)
        terminal.typed = session.typed
        terminal.state = state
        await once(lambda: pymux.wayland.display, 10.0, "the compositor never started")
        yield pymux


def environment(mux):
    return dict(os.environ, WAYLAND_DISPLAY=mux.wayland.display)


async def paste(mux, *flags):
    "wl-paste to its end; a paste nobody answers fails the test, not the run."
    with anyio.fail_after(15):
        result = await anyio.run_process(["wl-paste", "--no-newline", *flags], env=environment(mux), check=False)
    return result.returncode, result.stdout.decode()


@asynccontextmanager
async def copying(mux, text, *flags):
    "wl-copy, in the foreground so it holds the selection until the block ends."
    async with await anyio.open_process(["wl-copy", "--foreground", *flags, text], env=environment(mux)) as holder:
        try:
            yield holder
        finally:
            # Under "on" the compositor takes the selection back, and
            # wl-copy leaves on its own.
            with contextlib.suppress(ProcessLookupError):
                holder.kill()


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


async def environment_of_a_pane(mux, tmp_path) -> str:
    "What a new pane's program sees in WAYLAND_DISPLAY, or '' for nothing."
    out = tmp_path / "display"
    # Renamed into place: `write_text` makes the file before it fills it,
    # and a read in between saw an empty display.
    script = (
        "import os, pathlib; pathlib.Path(%r).write_text(os.environ.get('WAYLAND_DISPLAY', '')); os.replace(%r, %r)"
        % (str(out) + ".part", str(out) + ".part", str(out))
    )
    await mux.create_window("%s -c %s" % (sys.executable, _quoted(script)))
    await once(out.exists, 10.0, "the pane never wrote its environment")
    return out.read_text()


async def test_it_listens_beside_the_server_socket(mux, tmp_path):
    assert mux.wayland.display == str(tmp_path / "wayland-pymux.sock.test.sock")
    assert Path(mux.wayland.display).exists()


async def test_a_pane_with_no_display_gets_ours(mux, tmp_path):
    mux.global_environment.pop("WAYLAND_DISPLAY", None)
    os.environ.pop("WAYLAND_DISPLAY", None)

    assert await environment_of_a_pane(mux, tmp_path) == mux.wayland.display


async def test_a_session_with_a_display_keeps_it(mux, tmp_path):
    mux.global_environment["WAYLAND_DISPLAY"] = "wayland-desktop"

    assert await environment_of_a_pane(mux, tmp_path) == "wayland-desktop"


async def test_a_new_server_waits_for_the_old_one_to_let_go(mux, tmp_path):
    """
    `upgrade-server` starts the new build while the old one still holds
    the socket, and the old one lets go only when it exits.
    """
    path = mux.wayland.display
    successor = Pymux()
    successor.socket_name = mux.socket_name
    async with successor.running():
        await anyio.sleep(1.0)
        assert successor.wayland.display is None

        mux.handle_command("set-option wayland-server off")
        with anyio.fail_after(10):
            while successor.wayland.display != path:
                await anyio.sleep(0.05)
        successor.stop()


async def test_the_option_stops_and_starts_it(mux):
    path = mux.wayland.display
    mux.handle_command("set-option wayland-server off")
    await once(lambda: mux.wayland.display is None, 10.0, "it never stopped")
    assert not Path(path).exists()

    mux.handle_command("set-option wayland-server on")
    await once(lambda: mux.wayland.display == path, 10.0, "it never started again")


# ----------------------------------------------------------------------
# Under "external", the panes share their selections and nothing more.


async def test_panes_share_a_copy_under_external(mux):
    mux.clipboard_mode = Clipboard.EXTERNAL
    async with copying(mux, "between panes"):
        await once(lambda: mux.wayland.copied.get("clipboard"), 10.0, "the copy never arrived")
        assert await paste(mux) == (0, "between panes")
        assert mux.clipboard.get_data().text != "between panes"


async def test_a_paste_reads_nothing_of_the_person_under_external(mux):
    mux.clipboard_mode = Clipboard.EXTERNAL
    mux.clipboard.set_data(ClipboardData("private"))

    _code, text = await paste(mux)
    assert text == ""


# ----------------------------------------------------------------------
# Under "on", the person's terminal holds the truth.


async def test_a_paste_reads_the_desktop_clipboard(tmp_path):
    terminal = ClipboardTerminal()
    terminal.selections["c"] = "copied on the desktop"
    async with attached(tmp_path, terminal, Clipboard.ON) as mux:
        assert await paste(mux) == (0, "copied on the desktop")


async def test_a_paste_of_the_primary_reads_the_desktop_primary(tmp_path):
    terminal = ClipboardTerminal()
    terminal.selections["p"] = "selected on the desktop"
    async with attached(tmp_path, terminal, Clipboard.ON) as mux:
        assert await paste(mux, "--primary") == (0, "selected on the desktop")


async def test_a_pane_copy_goes_to_the_desktop_and_comes_back(tmp_path):
    terminal = ClipboardTerminal()
    async with attached(tmp_path, terminal, Clipboard.ON) as mux:
        async with copying(mux, "copied in a pane"):
            await once(lambda: terminal.selections.get("c") == "copied in a pane", 10.0, "never written")
            assert mux.clipboard.get_data().text == "copied in a pane"
            # The compositor took the selection back, so this asks the
            # terminal rather than the pane.
            assert await paste(mux) == (0, "copied in a pane")
            assert terminal.queries == 1


async def test_an_empty_desktop_clipboard_leaves_the_paste_buffer(tmp_path):
    async with attached(tmp_path, ClipboardTerminal(), Clipboard.ON) as mux:
        mux.clipboard.set_data(ClipboardData("copied in copy mode"))

        assert await paste(mux) == (0, "copied in copy mode")


async def test_a_silent_terminal_leaves_the_paste_buffer(tmp_path):
    terminal = ClipboardTerminal(answers=False)
    async with attached(tmp_path, terminal, Clipboard.ON) as mux:
        mux.clipboard.set_data(ClipboardData("copied in copy mode"))

        assert await paste(mux) == (0, "copied in copy mode")

        # Asked once; the next paste does not wait.
        with anyio.fail_after(CLIPBOARD_READ_SECONDS):
            assert await paste(mux) == (0, "copied in copy mode")
        assert terminal.queries == 1
