"""
The compositor a server holds for panes with no display, against real
wl-copy and wl-paste. Needs the `wayland` extra, so `checks.pymux-wayland`
runs this and `checks.pymux-unit` skips it. Lillecarl/pymux#383.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from prompt_toolkit.clipboard import ClipboardData
from session import once

pytest.importorskip("pysmithay")

from pymux.main import Pymux
from pymux.options import Clipboard


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


def environment(mux):
    return dict(os.environ, WAYLAND_DISPLAY=mux.wayland.display)


async def paste(mux):
    result = await anyio.run_process(["wl-paste", "--no-newline"], env=environment(mux), check=False)
    return result.returncode, result.stdout.decode()


async def environment_of_a_pane(mux, tmp_path) -> str:
    "What a new pane's program sees in WAYLAND_DISPLAY, or '' for nothing."
    out = tmp_path / "display"
    script = "import os, pathlib; pathlib.Path(%r).write_text(os.environ.get('WAYLAND_DISPLAY', ''))" % (str(out),)
    await mux.create_window("%s -c %s" % (sys.executable, _quoted(script)))
    await once(out.exists, 10.0, "the pane never wrote its environment")
    return out.read_text()


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


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


async def test_panes_share_one_clipboard_whatever_set_clipboard_says(mux):
    holder = subprocess.Popen(["wl-copy", "--foreground", "between panes"], env=environment(mux))
    try:
        await once(lambda: mux.wayland._from_panes, 10.0, "the copy never arrived")
        assert await paste(mux) == (0, "between panes")
    finally:
        holder.kill()
        holder.wait()


async def test_a_copy_reaches_the_paste_buffer_under_on(mux):
    mux.clipboard_mode = Clipboard.ON
    holder = subprocess.Popen(["wl-copy", "--foreground", "from a pane"], env=environment(mux))
    try:
        await once(lambda: mux.clipboard.get_data().text == "from a pane", 10.0, "the buffer never got it")
    finally:
        holder.kill()
        holder.wait()


async def test_a_copy_stays_in_the_panes_under_external(mux):
    mux.clipboard_mode = Clipboard.EXTERNAL
    holder = subprocess.Popen(["wl-copy", "--foreground", "kept inside"], env=environment(mux))
    try:
        await once(lambda: mux.wayland._from_panes == "kept inside", 10.0, "the copy never arrived")
        assert mux.clipboard.get_data().text != "kept inside"
    finally:
        holder.kill()
        holder.wait()


async def test_the_paste_buffer_reaches_the_panes_under_on(mux):
    mux.clipboard_mode = Clipboard.ON
    mux.clipboard.set_data(ClipboardData("copied in copy mode"))

    assert await paste(mux) == (0, "copied in copy mode")


async def test_the_paste_buffer_stays_out_under_external(mux):
    mux.clipboard_mode = Clipboard.EXTERNAL
    mux.clipboard.set_data(ClipboardData("private"))

    code, text = await paste(mux)
    assert text != "private"


async def test_the_option_stops_and_starts_it(mux):
    path = mux.wayland.display
    mux.handle_command("set-option wayland-server off")
    await once(lambda: mux.wayland.display is None, 10.0, "it never stopped")
    assert not Path(path).exists()

    mux.handle_command("set-option wayland-server on")
    await once(lambda: mux.wayland.display == path, 10.0, "it never started again")
