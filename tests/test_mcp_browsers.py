"""
pymux-mcp's browsers against a server that is really there: Chromium
headless as a job, and headful in a window pane. Lillecarl/pymux#822.

Runs in `checks.pymux-browsers`, which puts Chromium on the PATH.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from libpymux import Server

pytest.importorskip("pysmithay")
CHROMIUM = shutil.which("chromium")
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no chromium on the PATH")

from libpymux.connection import CommandResult
from pymux_mcp import browser
from pymux_mcp.api import OPERATIONS, Png

#: A build sandbox has no room for Chromium's own sandbox.
ARGUMENTS = ["--no-sandbox"]

PAGE = (
    "data:text/html;base64,"
    + base64.b64encode(
        b"<title>probe</title><input id=word aria-label=word>"
        b"<button onclick=\"console.log('clicked', document.getElementById('word').value);"
        b" document.title = 'done'\">Go</button>"
    ).decode()
)


@pytest.fixture
async def server(tmp_path):
    "A pymux server of its own, with its compositor."
    sock = str(tmp_path / "pymux.sock.browsers")
    env = {key: value for key, value in os.environ.items() if key not in ("PYMUX", "WAYLAND_DISPLAY")}
    async with await anyio.open_process(
        [sys.executable, "-m", "pymux", "start-server", "-S", sock],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        with anyio.fail_after(20):
            while not os.path.exists(sock):
                await anyio.sleep(0.05)
        found = Server(sock)
        try:
            yield found
        finally:
            await found.cmd("kill-server", check=False)
            with anyio.fail_after(30):
                await process.wait()


async def call(server: Server, op: str, **args):
    return await OPERATIONS.call(server, op, args)


def ref(line: str) -> str:
    "The ref a snapshot line carries."
    return line.rsplit("[ref=", 1)[1].rstrip("]")


@pytest.mark.parametrize("seat", ["headless", "pane"])
async def test_an_agent_fills_a_form_and_reads_what_the_page_logged(server, seat):
    opened = await call(server, "browser.open", name="probe", seat=seat, url=PAGE, arguments=ARGUMENTS)
    profile = Path(opened["profile"])
    assert ("window" if seat == "pane" else "job") in opened, opened
    assert [one["name"] for one in await call(server, "browser.list")] == ["probe"]

    snapshot = await call(server, "browser.snapshot", name="probe")
    word = next(line for line in snapshot.splitlines() if "textbox" in line)
    go = next(line for line in snapshot.splitlines() if "button" in line and '"Go"' in line)
    # Each operation connects anew, so a ref outlives the call that made it.
    await call(server, "browser.type", name="probe", text="hello", ref=ref(word))
    await call(server, "browser.click", name="probe", ref=ref(go))
    assert await call(server, "browser.evaluate", name="probe", expression="document.title") == "done"
    # Logged before this call connected: the browser replays it.
    said = await call(server, "browser.console", name="probe")
    assert {"type": "log", "text": "clicked hello"} in said, said

    shot = await call(server, "browser.screenshot", name="probe", fit=300)
    assert isinstance(shot, Png) and shot.data.startswith(b"\x89PNG")
    assert max(struct.unpack(">II", shot.data[16:24])) <= 300, shot.data[16:24]

    loaded = await call(server, "browser.goto", name="probe", url=PAGE)
    assert loaded["loaded"] and loaded["title"] == "probe", loaded
    (tab,) = await call(server, "browser.tabs", name="probe")
    assert tab["title"] == "probe", tab

    await call(server, "browser.close", name="probe")
    assert await call(server, "browser.list") == []
    assert not profile.exists()
    if seat == "pane":
        # Its window went with it, and no window of Chromium opened again.
        assert [one.name for one in await server.windows({"browser": None})] == []


async def test_a_second_browser_of_the_same_name_is_refused(server):
    await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS)
    try:
        with pytest.raises(ValueError, match="is open"):
            await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS)
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_ref_that_is_not_one_says_where_refs_come_from(server):
    await call(server, "browser.open", name="probe", seat="headless", url=PAGE, arguments=ARGUMENTS)
    try:
        with pytest.raises(ValueError, match=r"browser\.snapshot"):
            await call(server, "browser.click", name="probe", ref="button")
    finally:
        await call(server, "browser.close", name="probe")


async def test_the_collector_closes_a_browser_nobody_used(server):
    opened = await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS, idle=60)
    assert await browser.sweep(server, now=opened["used"] + 30) == []
    assert await browser.sweep(server, now=opened["used"] + 90) == ["probe"]
    assert await call(server, "browser.list") == []
    assert not Path(opened["profile"]).exists()


async def test_a_browser_operation_is_use(server):
    opened = await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS, idle=60)
    try:
        await anyio.sleep(1.5)
        await call(server, "browser.evaluate", name="probe", expression="1")
        (listed,) = await call(server, "browser.list")
        assert listed["used"] > opened["used"], (listed, opened)
    finally:
        await call(server, "browser.close", name="probe")


async def test_an_idle_of_nothing_is_kept_for_ever(server):
    opened = await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS, idle=0)
    try:
        assert await browser.sweep(server, now=opened["used"] + 10**6) == []
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_person_at_the_window_keeps_the_browser(server, monkeypatch):
    """
    A client that shows the browser's window and had a key or the mouse
    since: the companion of a person, though no agent called.
    """
    opened = await call(server, "browser.open", name="probe", seat="pane", url=PAGE, arguments=ARGUMENTS, idle=60)
    try:
        later = opened["used"] + 50
        cmd = server.cmd

        async def a_client_at_the_window(command, *args, **kwargs):
            if list(command)[:1] == ["list-clients"]:
                return CommandResult("@0 1\n%s %d\n" % (opened["window"], later), "", 0)
            return await cmd(command, *args, **kwargs)

        monkeypatch.setattr(server, "cmd", a_client_at_the_window)
        assert await browser.sweep(server, now=later + 30) == []
        (listed,) = await call(server, "browser.list")
        assert listed["used"] == later, listed
    finally:
        monkeypatch.undo()
        await call(server, "browser.close", name="probe")


def test_every_browser_operation_answers_json_an_agent_can_read():
    for name in ("browser.open", "browser.goto", "browser.snapshot", "browser.click", "browser.close"):
        assert json.dumps(OPERATIONS[name].schema())
