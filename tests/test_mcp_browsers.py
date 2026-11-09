"""
pymux-mcp's browsers against a server that is really there: Chromium
and qutebrowser headless as a job, and headful in a window pane.
Lillecarl/pyterm#824.

Runs in `checks.pymux-browsers`, which puts both on the PATH.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import psutil
import pytest
from anyio.abc import SocketAttribute
from libpymux import Server

pytest.importorskip("pysmithay")
CHROMIUM = shutil.which("chromium")
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no chromium on the PATH")

from libpymux.connection import CommandResult
from PIL import Image, ImageColor
from pymux_mcp import browser
from pymux_mcp.api import OPERATIONS, Png

from pymux import log

#: A build sandbox has no room for Chromium's own sandbox.
ARGUMENTS = ["--no-sandbox"]
#: The same switch, through qutebrowser to its QtWebEngine.
QUTEBROWSER_ARGUMENTS = ["--qt-flag=no-sandbox"]

needs_qutebrowser = pytest.mark.skipif(shutil.which("qutebrowser") is None, reason="no qutebrowser on the PATH")
needs_firefox = pytest.mark.skipif(shutil.which("firefox") is None, reason="no firefox on the PATH")
KINDS = [
    "chromium",
    pytest.param("qutebrowser", marks=needs_qutebrowser),
    pytest.param("firefox", marks=needs_firefox),
]


def arguments(kind: str) -> list[str]:
    "Each kind's way to leave its own sandbox off; Firefox's is `MOZ_DISABLE_CONTENT_SANDBOX`, set by the check."
    return {"chromium": ARGUMENTS, "qutebrowser": QUTEBROWSER_ARGUMENTS}.get(kind, [])


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
            started = time.monotonic()
            with anyio.move_on_after(10) as asking:
                await found.cmd("kill-server", check=False)
            with anyio.move_on_after(30):
                await process.wait()
            if process.returncode is None:
                # anyio's Process waits for ever on its way out, so a server
                # that does not end held the whole suite until its timeout.
                stacks = await _stacks_of(process)
                process.kill()
                await process.wait()
                pytest.fail("the server did not end at kill-server; its stacks:\n%s" % (stacks,))
            if asking.cancelled_caught:
                holders = [one.pid for one in psutil.process_iter() if _holds(one, sock)]
                pytest.fail(
                    "kill-server had no answer in 10s; the server ended %.1fs after it was asked;"
                    " processes with the socket open: %s" % (time.monotonic() - started, holders)
                )


def _holds(process: psutil.Process, sock: str) -> bool:
    try:
        return any(connection.laddr == sock for connection in process.net_connections("unix"))
    except psutil.Error:
        return False


async def _stacks_of(process: anyio.abc.Process) -> str:
    "What a server's SIGUSR1 dump says, beside its log."
    dump = log.default_logfile().parent / ("stacks-%d.log" % (process.pid,))
    process.send_signal(signal.SIGUSR1)
    await anyio.sleep(1)
    try:
        return dump.read_text()
    except OSError as why:
        return "none: %s" % (why,)


async def call(server: Server, op: str, **args):
    return await OPERATIONS.call(server, op, args)


def ref(line: str) -> str:
    "The ref a snapshot line carries."
    return line.rsplit("[ref=", 1)[1].rstrip("]")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("seat", ["headless", "pane"])
async def test_an_agent_fills_a_form_and_reads_what_the_page_logged(server, seat, kind):
    opened = await call(server, "browser.open", name="probe", kind=kind, seat=seat, url=PAGE, arguments=arguments(kind))
    profile = Path(opened["profile"])
    assert ("window" if seat == "pane" else "job") in opened, opened
    assert opened["kind"] == kind, opened
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

    # Less motion means fewer frames to send a person watching.
    assert await call(
        server, "browser.evaluate", name="probe", expression="matchMedia('(prefers-reduced-motion: reduce)').matches"
    )

    loaded = await call(server, "browser.goto", name="probe", url=PAGE)
    assert loaded["loaded"] and loaded["title"] == "probe", loaded
    (tab,) = await call(server, "browser.tabs", name="probe")
    assert tab["title"] == "probe", tab

    pid = browser._pid(profile, browser.Kind(kind))
    assert pid is not None
    await call(server, "browser.close", name="probe")
    assert await call(server, "browser.list") == []
    # A pane's window closes before the browser ends, which writes into
    # the profile until it does.
    assert not browser._alive(pid)
    assert not profile.exists()
    if seat == "pane":
        # Its window went with it, and no window of Chromium opened again.
        assert [one.name for one in await server.windows({"browser": None})] == []


async def test_a_browser_beside_a_persons_work_takes_only_its_own_panes_away(server):
    """
    A split of a window somebody works in. Chromium's second window
    opens beside its first, carries the same tags, and goes with it.
    """
    (window,) = await server.windows()
    (work,) = await window.panes()
    opened = await call(server, "browser.open", name="probe", url=PAGE, window=window.id, arguments=ARGUMENTS)
    assert opened["window"] == window.id and opened["pane"] != work.id, opened
    assert (await work.refresh()).active, "the browser took the keyboard"

    endpoint = (await browser.find(server, "probe")).endpoint()
    async with browser.connected(endpoint) as connection:
        await connection.send("Target.createTarget", {"url": PAGE, "newWindow": True})
    with anyio.fail_after(20):
        while len(await server.panes({"browser": "probe"})) < 2:
            await anyio.sleep(0.1)
    # A call finds the browser by any of its panes, and is use for all of them.
    assert await call(server, "browser.evaluate", name="probe", expression="1 + 1") == 2
    assert [one["name"] for one in await call(server, "browser.list")] == ["probe"]

    await call(server, "browser.close", name="probe")
    assert await server.panes({"browser": "probe"}) == []
    assert [pane.id for pane in await window.panes()] == [work.id]


@pytest.mark.parametrize(("kind", "directory"), [("chromium", "browsers"), ("qutebrowser", "qutebrowsers")])
async def test_a_kept_profile_outlives_its_browser(server, tmp_path, monkeypatch, kind, directory):
    "What the browser stored is there the next time, and one profile takes one browser at a time."
    if shutil.which(kind) is None:
        pytest.skip("no %s on the PATH" % (kind,))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    launch = {"kind": kind, "seat": "headless", "profile": "work", "arguments": arguments(kind)}
    opened = await call(server, "browser.open", name="probe", **launch)
    assert opened["kept"] and opened["profile"] == str(tmp_path / "state" / "pymux" / directory / "work")
    cookie = {"name": "kept", "value": "yes", "domain": "example.com", "path": "/", "expires": 2e9}
    try:
        with pytest.raises(ValueError, match="has the profile"):
            await call(server, "browser.open", name="other", **launch)
        async with browser.tab(server, "probe") as page:
            await page.send("Network.setCookie", cookie)
    finally:
        await call(server, "browser.close", name="probe")
    assert Path(opened["profile"]).is_dir()

    await call(server, "browser.open", name="probe", **launch)
    try:
        async with browser.tab(server, "probe") as page:
            cookies = (await page.send("Network.getCookies", {"urls": ["https://example.com/"]}))["cookies"]
        assert [(one["name"], one["value"]) for one in cookies] == [("kept", "yes")]
    finally:
        await call(server, "browser.close", name="probe")


@needs_firefox
async def test_firefox_is_reached_from_inside_and_keeps_its_profile(server, tmp_path, monkeypatch):
    """
    `gecko.chrome` runs in the browser window with chrome privilege; a
    preference it sets outlives the browser in a kept profile; and a ref
    outlives the connection that made it, as Chromium's does.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    launch = {"kind": "firefox", "seat": "headless", "profile": "work"}
    opened = await call(server, "browser.open", name="probe", url=PAGE, **launch)
    assert opened["profile"] == str(tmp_path / "state" / "pymux" / "firefoxes" / "work"), opened
    try:
        assert await call(server, "gecko.chrome", name="probe", code="gBrowser.tabs.length") == 1
        set_it = "Services.prefs.setStringPref('pymux.kept', 'yes'); (async () => 'set')()"
        assert await call(server, "gecko.chrome", name="probe", code=set_it) == "set"
        with pytest.raises(LookupError, match="nowhere is not defined"):
            await call(server, "gecko.chrome", name="probe", code="nowhere.at.all")
        snapshot = await call(server, "browser.snapshot", name="probe")
        go = next(line for line in snapshot.splitlines() if "button" in line and '"Go"' in line)
        assert ref(go) in await call(server, "browser.snapshot", name="probe")
    finally:
        await call(server, "browser.close", name="probe")

    await call(server, "browser.open", name="probe", **launch)
    try:
        assert (
            await call(server, "gecko.chrome", name="probe", code="Services.prefs.getStringPref('pymux.kept')") == "yes"
        )
    finally:
        await call(server, "browser.close", name="probe")


#: A second browser window, held where Firefox's startup has registered it
#: and not yet given it a `gBrowser`.
HALF_STARTED = """(async () => {
  const win = OpenBrowserWindow();
  await new Promise(resolve => win.addEventListener("load", resolve, {once: true}));
  Object.defineProperty(win, "gBrowser", {value: undefined, configurable: true, writable: true});
  return Services.wm.getMostRecentWindow("navigator:browser") === win ? "in front" : "behind";
})()"""


@needs_firefox
async def test_a_window_firefox_has_not_finished_is_left_out(server):
    "It is the front window, and its `gBrowser` is undefined: browser.open failed on it. Lillecarl/pyterm#947."
    await call(server, "browser.open", name="probe", url=PAGE, kind="firefox", seat="headless")
    try:
        assert await call(server, "gecko.chrome", name="probe", code=HALF_STARTED) == "in front"

        tabs = await call(server, "browser.tabs", name="probe")
        assert [tab["url"] for tab in tabs] == [PAGE], tabs
        went = await call(server, "browser.goto", name="probe", url=PAGE)
        assert went["loaded"], went
    finally:
        await call(server, "browser.close", name="probe")


@needs_firefox
async def test_firefox_applies_no_staged_update(server):
    "It would relaunch itself outside pymux. Lillecarl/pyterm#945."
    opened = await call(server, "browser.open", name="probe", kind="firefox", seat="headless")
    try:
        pid = browser._pid(Path(opened["profile"]), browser.Kind.FIREFOX)
        assert pid is not None
        assert psutil.Process(pid).environ().get("MOZ_DISABLE_UPDATE_PROCESSING") == "1"
    finally:
        await call(server, "browser.close", name="probe")


async def test_only_firefox_and_glide_have_a_chrome(server):
    await call(server, "browser.open", name="probe", seat="headless", arguments=ARGUMENTS)
    try:
        with pytest.raises(ValueError, match="only Firefox and Glide"):
            await call(server, "gecko.chrome", name="probe", code="1")
    finally:
        await call(server, "browser.close", name="probe")


#: A qutebrowser that will not quit: `close` has to kill it.
STUBBORN = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"


async def test_close_answers_once_the_holder_is_gone(server, tmp_path, monkeypatch):
    """
    The job ends after the browser's process does: here by a second, on a
    busy server by its reap. browser.list read it as open. Lillecarl/pyterm#966.
    """
    monkeypatch.setattr(browser, "QUIT_TIMEOUT", 0.5)
    profile = tmp_path / "profile"
    profile.mkdir()
    tags = {
        browser.NAME_TAG: "probe",
        browser.KIND_TAG: browser.Kind.QUTEBROWSER.value,
        browser.SEAT_TAG: browser.Seat.HEADLESS.value,
        browser.PROFILE_TAG: str(profile),
    }
    fake = shlex.join([sys.executable, "-c", STUBBORN, "--basedir=%s" % (profile,)])
    await server.run_job("trap '' TERM; %s; sleep 1" % (fake,), tags=tags)
    with anyio.fail_after(10):
        while browser._pid(profile, browser.Kind.QUTEBROWSER) is None:
            await anyio.sleep(0.05)

    with anyio.fail_after(10):
        await call(server, "browser.close", name="probe")
    assert await call(server, "browser.list") == []


def an_extension(directory: Path, name: str) -> str:
    "An unpacked extension whose service worker runs as long as it is loaded."
    directory.mkdir()
    manifest = {"manifest_version": 3, "name": name, "version": "1", "background": {"service_worker": "worker.js"}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "worker.js").write_text("self.addEventListener('install', () => {});\n")
    return str(directory)


async def workers(server: Server) -> list[str]:
    "The address of each extension's service worker, once each runs."
    endpoint = (await browser.find(server, "probe")).endpoint()
    async with browser.connected(endpoint) as connection:
        found = (await connection.send("Target.getTargets"))["targetInfos"]
    return sorted(one["url"] for one in found if one["type"] == "service_worker")


async def test_an_extension_loads_at_launch_and_into_a_running_browser(server, tmp_path):
    first = an_extension(tmp_path / "first", "first")
    await call(server, "browser.open", name="probe", seat="headless", extensions=[first], arguments=ARGUMENTS)
    try:
        with anyio.fail_after(20):
            while len(await workers(server)) < 1:
                await anyio.sleep(0.1)
        loaded = await call(
            server, "browser.extension", name="probe", directory=an_extension(tmp_path / "second", "second")
        )
        with anyio.fail_after(20):
            while len(await workers(server)) < 2:
                await anyio.sleep(0.1)
        assert "chrome-extension://%s/worker.js" % (loaded["id"],) in await workers(server)
        with pytest.raises(ValueError, match="not an unpacked extension"):
            await call(server, "browser.extension", name="probe", directory=str(tmp_path))
    finally:
        await call(server, "browser.close", name="probe")


@needs_qutebrowser
@pytest.mark.parametrize("profile", [None, "kept"])
async def test_an_agent_reaches_into_qutebrowser_through_its_bridge(server, tmp_path, monkeypatch, profile):
    """
    Python and commands run inside the browser, with what it said. A
    kept profile under this test's long temporary path is too long for a
    socket, so its bridge serves from the runtime directory.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    opened = await call(
        server,
        "browser.open",
        name="probe",
        kind="qutebrowser",
        seat="headless",
        url=PAGE,
        profile=profile,
        arguments=QUTEBROWSER_ARGUMENTS,
    )
    try:
        said = await call(server, "qutebrowser.python", name="probe", code="config.val.zoom.default")
        assert said == {"value": 100.0, "error": None, "stdout": "", "messages": []}, said
        said = await call(server, "qutebrowser.command", name="probe", command="set zoom.default 125%")
        assert said["error"] is None, said
        said = await call(server, "qutebrowser.python", name="probe", code="print('hi'); tab().url().toString()")
        assert (said["value"], said["stdout"]) == (PAGE, "hi\n"), said
        said = await call(
            server, "qutebrowser.python", name="probe", code="message.info('seen'); config.val.zoom.default"
        )
        assert said["value"] == 125.0 and said["messages"] == [{"level": "info", "text": "seen"}], said
        said = await call(server, "qutebrowser.command", name="probe", command="no-such-command")
        assert "no such command" in said["error"], said
    finally:
        await call(server, "browser.close", name="probe")
    assert not browser.bridge.socket_for(Path(opened["profile"])).exists()


@needs_qutebrowser
async def test_qutebrowser_refuses_what_only_chromium_has(server, tmp_path):
    "No extensions and no chrome://settings, said plainly rather than as a protocol error."
    extension = an_extension(tmp_path / "first", "first")
    with pytest.raises(ValueError, match="only Chromium"):
        await call(server, "browser.open", name="probe", kind="qutebrowser", seat="headless", extensions=[extension])
    await call(
        server, "browser.open", name="probe", kind="qutebrowser", seat="headless", arguments=QUTEBROWSER_ARGUMENTS
    )
    try:
        with pytest.raises(ValueError, match="only Chromium"):
            await call(server, "browser.extension", name="probe", directory=extension)
        with pytest.raises(ValueError, match="only Chromium"):
            await call(server, "browser.setting", name="probe", key="translate_blocked_languages")
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_setting_is_read_and_set_without_taking_the_front(server):
    await call(server, "browser.open", name="probe", seat="headless", url=PAGE, arguments=ARGUMENTS)
    try:
        key = "translate_blocked_languages"
        assert "sv" not in await call(server, "browser.setting", name="probe", key=key)
        assert await call(server, "browser.setting", name="probe", key=key, value=["en", "sv"]) == ["en", "sv"]
        assert await call(server, "browser.setting", name="probe", key=key) == ["en", "sv"]
        with pytest.raises((LookupError, browser.CDPError)):
            await call(server, "browser.setting", name="probe", key="no.such.setting")
        # The settings tab came and went; the page is the one tab.
        assert [tab["title"] for tab in await call(server, "browser.tabs", name="probe")] == ["probe"]
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_marked_picture_names_what_it_shows_by_ref(server):
    "An agent picks a ref off the picture, and click takes it: no snapshot of the page."
    await call(server, "browser.open", name="probe", seat="headless", url=PAGE, arguments=ARGUMENTS)
    try:
        shot = await call(server, "browser.screenshot", name="probe", marks=True, fit=None)
        lines = shot.caption.splitlines()
        snapshot = await call(server, "browser.snapshot", name="probe")
        go = ref(next(line for line in snapshot.splitlines() if '"Go"' in line))
        word = ref(next(line for line in snapshot.splitlines() if "textbox" in line))
        assert '%s button "Go"' % (go,) in lines, lines
        assert '%s textbox "word"' % (word,) in lines, lines

        # The box is drawn where the button is, in a mark colour.
        async with browser.tab(server, "probe") as page:
            (button,) = [mark for mark in await browser.marks(page) if mark.ref == go]
        picture = Image.open(io.BytesIO(shot.data)).convert("RGB")
        colours = {ImageColor.getrgb(colour) for colour in browser.MARK_COLOURS}
        assert picture.getpixel((int(button.x) + 1, int(button.y + button.height / 2))) in colours

        await call(server, "browser.click", name="probe", ref=go)
        assert await call(server, "browser.evaluate", name="probe", expression="document.title") == "done"
        plain = await call(server, "browser.screenshot", name="probe", fit=None)
        assert plain.caption == ""
    finally:
        await call(server, "browser.close", name="probe")


COVERED_PAGE = (
    "data:text/html;base64,"
    + base64.b64encode(
        b"<title>covered</title><style>button{display:block;margin:40px}</style>"
        b"<button>Under</button><div style='height:200px'></div><button>Clear</button>"
        b"<div role=dialog style='position:fixed;top:0;left:0;right:0;height:150px;background:#fff'>"
        b"<button>Accept</button></div>"
    ).decode()
)


async def test_a_mark_goes_only_on_what_a_click_reaches(server):
    "A dialog over the page covers the button under it: a click there goes to the dialog."
    await call(server, "browser.open", name="probe", seat="headless", url=COVERED_PAGE, arguments=ARGUMENTS)
    try:
        shot = await call(server, "browser.screenshot", name="probe", marks=True, fit=None)
        named = [line.split(" ", 2)[2] for line in shot.caption.splitlines()]
        assert '"Accept"' in named and '"Clear"' in named, named
        assert '"Under"' not in named, named
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_point_on_the_picture_names_the_element_there(server):
    await call(server, "browser.open", name="probe", seat="headless", url=PAGE, arguments=ARGUMENTS)
    try:
        async with browser.tab(server, "probe") as page:
            (button,) = [mark for mark in await browser.marks(page) if mark.role == "button"]
        x, y = button.x + button.width / 2, button.y + button.height / 2
        found = await call(server, "browser.at", name="probe", x=x, y=y)
        assert (found["ref"], found["role"], found["name"]) == (button.ref, "button", "Go"), found

        # Read off a picture drawn at a quarter of the page's size.
        shot = await call(server, "browser.screenshot", name="probe", fit=300)
        size = Image.open(io.BytesIO(shot.data)).size
        async with browser.tab(server, "probe") as page:
            view = (await page.send("Page.getLayoutMetrics"))["cssVisualViewport"]
        scale = size[0] / view["clientWidth"]
        found = await call(server, "browser.at", name="probe", x=x * scale, y=y * scale, of=list(size))
        assert found["ref"] == button.ref, found

        with pytest.raises(LookupError, match="nothing the page names"):
            await call(server, "browser.at", name="probe", x=view["clientWidth"] - 5, y=view["clientHeight"] - 5)

        # A click at the same point of the picture hits the button.
        hit = await call(server, "browser.click", name="probe", x=x * scale, y=y * scale, of=list(size))
        assert hit["ref"] == button.ref, hit
        assert await call(server, "browser.evaluate", name="probe", expression="document.title") == "done"
        with pytest.raises(ValueError, match="both"):
            await call(server, "browser.click", name="probe", x=x)
    finally:
        await call(server, "browser.close", name="probe")


LONG_PAGE = (
    "data:text/html;base64,"
    + base64.b64encode(
        b"<title>long</title><nav><a href='#a'>Home</a>"
        b" <a href='#b' onclick=\"document.title = 'contact'; return false\">Contact us</a></nav>"
        + b"".join(b"<p>Filler paragraph %d.</p>" % (number,) for number in range(200))
        + b"<form aria-label=order><input aria-label=email><button type=button>Order now</button></form>"
    ).decode()
)


async def test_open_answers_once_the_page_loaded(server):
    """
    Chromium listens before the page on its command line begins to load,
    so a look as soon as it listens finds a blank page: no marks, no tree.
    """
    opened = await call(server, "browser.open", name="probe", seat="headless", url=LONG_PAGE, arguments=ARGUMENTS)
    try:
        assert opened["page"]["loaded"] and opened["page"]["title"] == "long", opened
    finally:
        await call(server, "browser.close", name="probe")

    with pytest.raises(browser.CDPError, match="is open, but"):
        await call(
            server, "browser.open", name="probe", seat="headless", url="http://127.0.0.1:9/", arguments=ARGUMENTS
        )
    try:
        assert [one["name"] for one in await call(server, "browser.list")] == ["probe"]
    finally:
        await call(server, "browser.close", name="probe")


async def test_find_answers_only_the_elements_that_read_the_text(server):
    await call(server, "browser.open", name="probe", seat="headless", url=LONG_PAGE, arguments=ARGUMENTS)
    try:
        found = (await call(server, "browser.find", name="probe", text="contact")).splitlines()
        assert len(found) == 1 and found[0].startswith('- link "Contact us" [ref=e'), found
        # A match in running text answers the element holding it.
        (filler,) = (await call(server, "browser.find", name="probe", text="paragraph 150.")).splitlines()
        assert filler.startswith('- paragraph "Filler paragraph 150."'), filler
        assert await call(server, "browser.find", name="probe", text="nowhere") == 'nothing reads "nowhere"'

        await call(server, "browser.click", name="probe", ref=ref(found[0]))
        assert await call(server, "browser.evaluate", name="probe", expression="document.title") == "contact"

        # One form of a long page, without the two hundred paragraphs.
        (form,) = (await call(server, "browser.find", name="probe", text="order")).splitlines()[:1]
        part = await call(server, "browser.snapshot", name="probe", within=ref(form))
        assert '"Order now"' in part and "email" in part and "Filler" not in part, part
    finally:
        await call(server, "browser.close", name="probe")


async def test_the_agent_reads_what_the_person_selected(server):
    await call(server, "browser.open", name="probe", seat="headless", url=LONG_PAGE, arguments=ARGUMENTS)
    try:
        assert await call(server, "browser.selection", name="probe") == "nothing is selected"
        (paragraph,) = (await call(server, "browser.find", name="probe", text="paragraph 7.")).splitlines()
        await call(
            server,
            "browser.evaluate",
            name="probe",
            expression=(
                "(() => { const text = document.querySelectorAll('p')[7].firstChild;"
                " const range = document.createRange(); range.setStart(text, 7); range.setEnd(text, 16);"
                " getSelection().removeAllRanges(); getSelection().addRange(range); })()"
            ),
        )
        found = await call(server, "browser.selection", name="probe")
        assert found == {"text": "paragraph", "ref": ref(paragraph), "role": "paragraph", "name": ""}, found

        # In a text field the page's selection is empty; the field's is not.
        await call(server, "browser.type", name="probe", text="me@example.com", selector="input")
        await call(
            server,
            "browser.evaluate",
            name="probe",
            expression="document.querySelector('input').setSelectionRange(3, 10)",
        )
        found = await call(server, "browser.selection", name="probe")
        assert (found["text"], found["role"], found["name"]) == ("example", "textbox", "email"), found
    finally:
        await call(server, "browser.close", name="probe")


async def test_pointing_shows_the_person_an_element_for_a_while(server):
    await call(server, "browser.open", name="probe", seat="headless", url=LONG_PAGE, arguments=ARGUMENTS)
    try:
        (order,) = [line for line in (await call(server, "browser.find", name="probe", text="order now")).splitlines()]
        await call(server, "browser.point", name="probe", ref=ref(order), label="this one", seconds=1)
        shown = "document.querySelector('[data-pymux-point]')?.textContent ?? null"
        assert await call(server, "browser.evaluate", name="probe", expression=shown) == "this one"
        # Scrolled to: the button is in view, past two hundred paragraphs.
        in_view = "document.querySelector('button').getBoundingClientRect().top < innerHeight"
        assert await call(server, "browser.evaluate", name="probe", expression=in_view)
        # It covers nothing a person clicks.
        hit = await call(server, "browser.click", name="probe", ref=ref(order))
        assert hit["ref"] == ref(order), hit
        with anyio.fail_after(5):
            while await call(server, "browser.evaluate", name="probe", expression=shown) is not None:
                await anyio.sleep(0.2)
    finally:
        await call(server, "browser.close", name="probe")


async def test_a_profile_name_is_one_path_part(server):
    with pytest.raises(ValueError, match="not a profile name"):
        await call(server, "browser.open", name="probe", seat="headless", profile="../escape", arguments=ARGUMENTS)


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


# ----------------------------------------------------------------------
# A browser through the SOCKS proxy a client forwards. Lillecarl/pyterm#975.

#: A host name nothing on this machine resolves. Only the far side of
#: the proxy knows it, so a page from it loads only through the proxy,
#: and only from a browser that sent the proxy the name.
FAR_SIDE = "far-side.test"


@pytest.mark.parametrize("kind", KINDS)
async def test_a_browser_through_socks_loads_what_only_the_far_side_resolves(server, monkeypatch, kind):
    """
    The proxy is pymux's own SOCKS answer, the one a client runs for
    `forward-port -R 1080`, so each kind is judged against the reader
    it meets in use.
    """
    import asyncio

    from pymux.client import socks

    reached = []

    async def the_far_side(host: str, port: int):
        reached.append(host)
        return await anyio.connect_tcp("127.0.0.1", page_port)

    monkeypatch.setattr(socks, "connect_tcp", the_far_side)

    with anyio.fail_after(60):
        async with _a_page_server() as page_port:
            # asyncio's streams, because the answer reads asyncssh's,
            # which have their shape.
            proxy = await asyncio.start_server(socks.answer, "127.0.0.1", 0)
            try:
                address = "127.0.0.1:%d" % (proxy.sockets[0].getsockname()[1],)
                await browser.open_browser(
                    server,
                    "far",
                    browser.Seat.HEADLESS,
                    None,
                    None,
                    arguments(kind),
                    kind=browser.Kind(kind),
                    proxy=address,
                )
                try:
                    await call(server, "browser.goto", name="far", url="http://%s/" % (FAR_SIDE,), timeout=30)
                    title = await call(server, "browser.evaluate", name="far", expression="document.title")
                finally:
                    await call(server, "browser.close", name="far")
            finally:
                proxy.close()

    assert title == "far side"
    assert FAR_SIDE in reached, reached


@asynccontextmanager
async def _a_page_server():
    "An HTTP server on loopback whose every page is titled `far side`."

    async def handle(stream) -> None:
        async with stream:
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += await stream.receive()
                body = b"<title>far side</title>"
                await stream.send(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n"
                    b"Connection: close\r\n\r\n%s" % (len(body), body)
                )
            except anyio.EndOfStream, anyio.BrokenResourceError:
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, handle)
            try:
                yield listener.extra(SocketAttribute.local_address)[1]
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await listener.aclose()
