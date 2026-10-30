"""
Chromium for an agent to drive, over the DevTools protocol, where a
person can watch it. Lillecarl/pymux#822.

A browser runs in one of two seats:

- `headless`: a job of the server, with nothing to see.
- `pane`: a window pane of the server, through `wl`. Anybody attached
  watches it, over SSH too. It opens in a window of its own, or as a
  split of a window somebody works in.

**pymux holds the browser, and this process holds nothing.** Each
operation finds the browser by its tag, reads its port out of
`DevToolsActivePort` in its profile, connects, acts and disconnects. So
a browser outlives the pymux-mcp that opened it, and two agents can
share one. Measured on Chromium 155, the two things a held connection
would otherwise have to keep survive a reconnect: `Runtime.enable`
replays the console, and a node keeps its `backendDOMNodeId`, which a
ref names.

The address is the server machine's loopback, so pymux-mcp has to run
on that machine.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, NamedTuple

import anyio
from libpymux import Job, Pane, Server, Window
from PIL import Image, ImageDraw, ImageFont

from .cdp import CDPError, Connection, Page, connected

__all__ = ["Browser", "Seat", "close", "collect", "find", "open_browser", "sweep", "tab"]

#: The tag a browser's job or pane carries, with the browser's name.
NAME_TAG: Final = "browser"
#: The tag holding the profile, where the port is written.
PROFILE_TAG: Final = "browser-profile"
SEAT_TAG: Final = "browser-seat"
#: When an agent or a person last used it, in Unix seconds.
USED_TAG: Final = "browser-used"
#: How many seconds unused before the collector closes it; 0 for never.
IDLE_TAG: Final = "browser-idle"

#: What a browser left unused for longer than this is closed by default:
#: long enough to read a page beside an agent, short enough that a
#: browser an agent forgot does not run for a day.
DEFAULT_IDLE: Final = 1800.0
#: How often the collector looks.
SWEEP_SECONDS: Final = 30.0

#: How long Chromium may take to listen.
LISTEN_TIMEOUT: Final = 30.0

#: A kept profile's name: one path part, and no hidden file.
PROFILE_NAME: Final = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
#: Where Chromium writes the port it listens on, in its profile.
PORT_FILE: Final = "DevToolsActivePort"

#: Every request home and first-run page off: a page an agent did not
#: ask for covers the one it did.
QUIET: Final = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "--password-store=basic",
)

#: Less motion, so a watched browser sends fewer frames: every one
#: crosses to each client that shows it. Measured on Chromium 155: the
#: switch makes `prefers-reduced-motion: reduce` match, and a site that
#: asks for the `Sec-CH-Prefers-Reduced-Motion` hint gets the same.
CALM: Final = (
    "--force-prefers-reduced-motion",
    "--disable-smooth-scrolling",
)

#: What lets `Extensions.loadUnpacked` load an extension into a running
#: browser; without it the method is not there. Measured on Chromium 155.
EXTENSIBLE: Final = ("--enable-unsafe-extension-debugging",)


class Seat(StrEnum):
    HEADLESS = "headless"
    PANE = "pane"


class BrowserGone(LookupError):
    "No browser by that name, or one that stopped listening."


class Browser:
    """
    A browser that pymux holds, and its profile.

    A `pane` browser in a window of its own is held by that window and
    not by its pane: a pane shows one window of Chromium, and Chromium
    opens the next one beside it in the same window. One that split a
    window somebody else uses is held by its panes, which pymux tags
    alike, and `holder` is the first of them.
    """

    def __init__(
        self, name: str, seat: Seat, profile: Path, holder: Job | Window | Pane, used: float, idle: float
    ) -> None:
        self.name = name
        self.seat = seat
        self.profile = profile
        self.holder = holder
        self.used = used
        self.idle = idle

    def json(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "name": self.name,
            "seat": self.seat.value,
            "profile": str(self.profile),
            "used": int(self.used),
            "idle": self.idle,
            "kept": kept(self.profile),
        }
        if isinstance(self.holder, Job):
            found["job"] = self.holder.id
        elif isinstance(self.holder, Pane):
            found["pane"] = self.holder.id
            found["window"] = self.holder.window_id
        else:
            found["window"] = self.holder.id
        return found

    @property
    def window_id(self) -> str | None:
        "The pymux window that shows it, if any."
        if isinstance(self.holder, Pane):
            return self.holder.window_id
        return self.holder.id if isinstance(self.holder, Window) else None

    def endpoint(self) -> str | None:
        "The browser's WebSocket, or None before it listens."
        try:
            port, path = (self.profile / PORT_FILE).read_text().split()
        except OSError, ValueError:
            return None
        return "ws://127.0.0.1:%s%s" % (port, path)


def profiles() -> Path:
    "Where kept profiles live."
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "pymux" / "browsers"


def kept(profile: Path) -> bool:
    "A profile `close` leaves where it is: one with a name."
    return profile.parent == profiles()


def chromium(executable: str | None) -> str:
    found = executable or os.environ.get("PYMUX_MCP_CHROMIUM") or shutil.which("chromium")
    if not found:
        raise LookupError("no Chromium: name `executable`, set PYMUX_MCP_CHROMIUM, or put chromium on the PATH")
    return found


async def find(server: Server, name: str) -> Browser:
    "The browser called `name`, from the tags of what holds it."
    for found in await _holders(server, {NAME_TAG: name}):
        return await _browser(server, found)
    raise BrowserGone("no browser named %r; browser.open makes one, browser.list shows them" % (name,))


async def every(server: Server) -> list[Browser]:
    return [await _browser(server, holder) for holder in await _holders(server, {NAME_TAG: None})]


async def _holders(server: Server, wanted: Mapping[str, str | None]) -> list[Job | Window | Pane]:
    "What holds each browser: one holder a name, though a split browser has a pane for each of its windows."
    found: list[Job | Window | Pane] = [job for job in await server.jobs(wanted) if job.running]
    found += await server.windows(wanted)
    found += await server.panes(wanted)
    tags = await server.tags()
    named: set[str | None] = set()
    holders = []
    for holder in found:
        name = (holder.tags if isinstance(holder, Job) else tags.get(holder.id) or {}).get(NAME_TAG)
        if name not in named:
            named.add(name)
            holders.append(holder)
    return holders


async def _panes(server: Server, browser: Browser) -> list[Pane]:
    "Every pane of a split browser."
    return await server.panes({NAME_TAG: browser.name})


async def _browser(server: Server, holder: Job | Window | Pane) -> Browser:
    tags = holder.tags if isinstance(holder, Job) else (await server.tags()).get(holder.id, {})
    return Browser(
        tags[NAME_TAG] or "",
        Seat(tags[SEAT_TAG]),
        Path(tags[PROFILE_TAG] or ""),
        holder,
        # A browser with no mark of use counts as used now, rather than
        # as unused since 1970.
        float(tags.get(USED_TAG) or time.time()),
        float(tags.get(IDLE_TAG) or DEFAULT_IDLE),
    )


async def touch(server: Server, browser: Browser, when: float | None = None) -> None:
    "Mark the browser used, at `when` or now."
    browser.used = time.time() if when is None else when
    used = {USED_TAG: str(int(browser.used))}
    if isinstance(browser.holder, Pane):
        # Every pane: whichever is first when the next call looks reads it.
        for pane in await _panes(server, browser):
            await pane.set_tags(used)
    else:
        await server.set_tags(browser.holder.id, used)


async def open_browser(
    server: Server,
    name: str,
    seat: Seat,
    url: str,
    session: str | None,
    executable: str | None,
    arguments: Sequence[str],
    idle: float = DEFAULT_IDLE,
    window: str | None = None,
    profile_name: str | None = None,
    extensions: Sequence[str] = (),
) -> Browser:
    """
    Start a browser in `seat`, and answer it once it listens: in a split
    of `window` when one is named, on the kept profile `profile_name`
    when one is named, and with the unpacked `extensions` loaded.
    """
    for directory in extensions:
        _an_extension(directory)
    if window is not None and seat is not Seat.PANE:
        raise ValueError("only a `pane` browser opens in a window")
    if profile_name is not None and not PROFILE_NAME.fullmatch(profile_name):
        raise ValueError("%r is not a profile name: letters, digits, `.`, `_` and `-`" % (profile_name,))
    profile = None if profile_name is None else profiles() / profile_name
    for existing in await every(server):
        if existing.name == name:
            raise ValueError("a browser named %r is open; close it or pick another name" % (name,))
        # A second Chromium on a profile hands its launch to the first and ends.
        if existing.profile == profile:
            raise ValueError("the browser %r has the profile %r open" % (existing.name, profile_name))
    if profile is None:
        profile = Path(tempfile.mkdtemp(prefix="pymux-browser-"))
    else:
        profile.mkdir(parents=True, exist_ok=True)
        # The last run's port, which would read as listening at once.
        (profile / PORT_FILE).unlink(missing_ok=True)
    used = time.time()
    tags = {
        NAME_TAG: name,
        SEAT_TAG: seat.value,
        PROFILE_TAG: str(profile),
        USED_TAG: str(int(used)),
        IDLE_TAG: "%g" % (idle,),
    }
    argv = [
        "--remote-debugging-port=0",
        "--user-data-dir=%s" % (profile,),
        *QUIET,
        *CALM,
        *EXTENSIBLE,
        *(["--load-extension=%s" % (",".join(extensions),)] if extensions else []),
        *arguments,
    ]
    holder: Job | Window | Pane
    try:
        if seat is Seat.HEADLESS:
            command = [chromium(executable), "--headless=new", *argv, url]
            holder = await server.run_job(shlex.join(command), tags=tags)
        else:
            command = [chromium(executable), "--ozone-platform=wayland", *argv, url]
            if window is None:
                holder = await _in_a_window(server, name, session, command, tags)
            else:
                holder = await _beside(server, window, command, tags)
        browser = Browser(name, seat, profile, holder, used, idle)
        await _listening(browser)
    except BaseException:
        _forget(profile)
        raise
    return browser


async def _in_a_window(
    server: Server, name: str, session: str | None, command: Sequence[str], tags: Mapping[str, str]
) -> Window:
    found = await (server.session() if session is None else server.session_named(session))
    if found is None:
        raise LookupError("no session named %r" % (session,))
    # Not in front: the person looks when they choose to, and an agent
    # opening a browser must not take the screen they are working in.
    window = await found.new_window(name="browser %s" % (name,), select=False, tags=tags)
    if window is None:
        raise LookupError("new-window answered nothing")
    (pane,) = await window.panes()
    await _wl(server, pane, command, window)
    return window


async def _beside(server: Server, window: str, command: Sequence[str], tags: Mapping[str, str]) -> Pane:
    "A split of a window somebody works in; the keyboard stays where it was."
    split = await Window(server, {"window_id": window}).split(vertical=False, select=False, tags=tags)
    if split is None:
        raise LookupError("no window %s" % (window,))
    await _wl(server, split, command, split)
    # The window pane took the split's place, and its tags, under an id of its own.
    return (await server.panes({NAME_TAG: tags[NAME_TAG]}))[0]


async def _wl(server: Server, pane: Pane, command: Sequence[str], holder: Window | Pane) -> None:
    # `wl` answers once the window opened, or with what the program said
    # when it ended first.
    result = await server.cmd(["wl", "-t", pane.id, "--", *command], check=False)
    if result.exit_code != 0:
        await holder.kill()
        raise CDPError("chromium ended before it opened a window: %s" % ((result.stdout + result.stderr).strip(),))


async def _listening(browser: Browser) -> None:
    try:
        with anyio.fail_after(LISTEN_TIMEOUT):
            while browser.endpoint() is None:
                if isinstance(browser.holder, Job):
                    await browser.holder.refresh()
                    if not browser.holder.running:
                        said = await browser.holder.output(stderr=True)
                        raise CDPError("the browser ended before it listened: %s" % (said.strip()[-2000:],))
                await anyio.sleep(0.1)
    except TimeoutError:
        await close(browser)
        raise CDPError("the browser did not listen in %gs" % (LISTEN_TIMEOUT,)) from None


#: How long a browser asked to quit may take before its holder is killed.
QUIT_TIMEOUT: Final = 10.0


async def close(browser: Browser) -> None:
    """
    Stop the browser, and take its holder away, and its profile unless
    it is kept.

    Chromium is asked to quit first. Killing a window pane closes one of
    its windows and nothing more, and the next one opens in its place.

    The profile goes only once Chromium's process ended: its last window
    closes before it does, and it writes into the profile until then.
    """
    pid = _pid(browser.profile)
    try:
        endpoint = browser.endpoint()
        if endpoint is not None:
            with anyio.move_on_after(QUIT_TIMEOUT):
                try:
                    async with connected(endpoint) as connection:
                        await connection.send("Browser.close")
                except CDPError, OSError:
                    pass
                while await _held(browser):
                    await anyio.sleep(0.1)
        if isinstance(browser.holder, Pane):
            # Its own panes, never the window: somebody else works there.
            for pane in await _panes(browser.holder.server, browser):
                await pane.kill()
        elif await _held(browser):
            await browser.holder.kill()
        if pid is not None:
            with anyio.move_on_after(QUIT_TIMEOUT):
                while _alive(pid):
                    await anyio.sleep(0.1)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    finally:
        _forget(browser.profile)


def _pid(profile: Path) -> int | None:
    "The process that has the profile open, from the `host-pid` Chromium links its `SingletonLock` to."
    try:
        _host, _, pid = os.readlink(profile / "SingletonLock").rpartition("-")
    except OSError:
        return None
    return int(pid) if pid.isdigit() else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _an_extension(directory: str) -> None:
    if not os.path.isabs(directory) or not os.path.isfile(os.path.join(directory, "manifest.json")):
        raise ValueError("%r is not an unpacked extension: an absolute directory with a manifest.json" % (directory,))
    if "," in directory:
        raise ValueError("%r: Chromium reads a comma as the end of an extension's directory" % (directory,))


async def load_extension(browser: Browser, directory: str) -> str:
    "Load an unpacked extension into the running browser, and answer its id."
    _an_extension(directory)
    endpoint = browser.endpoint()
    if endpoint is None:
        raise BrowserGone("the browser %r is not listening" % (browser.name,))
    async with connected(endpoint) as connection:
        answer = await connection.send("Extensions.loadUnpacked", {"path": directory})
    return answer["id"]


#: The page whose script may read and set every preference, through
#: `chrome.settingsPrivate`; no DevTools method reaches them.
SETTINGS_PAGE: Final = "chrome://settings/"
SETTINGS_TIMEOUT: Final = 10.0


async def setting(browser: Browser, key: str, value: Any = None, change: bool = False) -> Any:
    """
    A preference of the running browser by its key, such as
    `translate_blocked_languages`; set to `value` first when `change`.

    In a tab opened in the background and closed after, so the tab a
    person reads stays in front.
    """
    endpoint = browser.endpoint()
    if endpoint is None:
        raise BrowserGone("the browser %r is not listening" % (browser.name,))
    async with connected(endpoint) as connection:
        opened = await connection.send("Target.createTarget", {"url": SETTINGS_PAGE, "background": True})
        try:
            page = await Page.attach(connection, opened["targetId"])
            ready = "typeof chrome === 'object' && !!chrome.settingsPrivate"
            with anyio.fail_after(SETTINGS_TIMEOUT):
                while not await evaluate(page, ready):
                    await anyio.sleep(0.1)
            if change:
                done = await evaluate(
                    page, "chrome.settingsPrivate.setPref(%s, %s)" % (json.dumps(key), json.dumps(value))
                )
                if not done:
                    raise ValueError("Chromium would not set %s to %s" % (key, json.dumps(value)))
            found = await evaluate(page, "chrome.settingsPrivate.getPref(%s)" % (json.dumps(key),))
            if not found:
                raise LookupError("Chromium has no setting %s" % (key,))
            return found["value"]
        finally:
            await connection.send("Target.closeTarget", {"targetId": opened["targetId"]})


def _forget(profile: Path) -> None:
    if not kept(profile):
        shutil.rmtree(profile, ignore_errors=True)


async def sweep(server: Server, now: float | None = None) -> list[str]:
    """
    Close every browser unused for longer than its idle limit, and
    answer their names.

    A window a client shows counts as used when a person pressed a key
    or moved the mouse at that client: it is somebody's companion, not
    the agent's alone.
    """
    now = time.time() if now is None else now
    shown: dict[str, float] = {}
    listed = await server.cmd(["list-clients", "-F", "#{window_id} #{client_activity}"], check=False)
    for line in listed.stdout.splitlines():
        window, _, activity = line.partition(" ")
        if activity.isdigit():
            shown[window] = max(shown.get(window, 0.0), float(activity))
    closed = []
    for browser in await every(server):
        seen = shown.get(browser.window_id or "", 0.0)
        if seen > browser.used:
            await touch(server, browser, seen)
        if browser.idle and now - browser.used > browser.idle:
            await close(browser)
            closed.append(browser.name)
    return closed


async def collect(server_for: Callable[[], Awaitable[Server]]) -> None:
    "Sweep every `SWEEP_SECONDS`, for as long as this process serves."
    while True:
        await anyio.sleep(SWEEP_SECONDS)
        # A server that is gone now is the next sweep's to find again; a
        # sweep that failed must not end the ones after it.
        with contextlib.suppress(Exception):
            await sweep(await server_for())


async def _held(browser: Browser) -> bool:
    if isinstance(browser.holder, Pane):
        return bool(await _panes(browser.holder.server, browser))
    try:
        await browser.holder.refresh()
    except LookupError:
        return False
    return not isinstance(browser.holder, Job) or browser.holder.running


@asynccontextmanager
async def tab(server: Server, name: str, target: str | None = None) -> AsyncIterator[Page]:
    "A tab of the browser called `name`, attached to for the block: `target`, or the first one."
    browser = await find(server, name)
    endpoint = browser.endpoint()
    if endpoint is None:
        raise BrowserGone("the browser %r is not listening" % (name,))
    await touch(server, browser)
    async with connected(endpoint) as connection:
        found = await tabs(connection)
        if target is not None:
            found = [one for one in found if one["targetId"] == target]
        if not found:
            raise BrowserGone("the browser %r has no tab %s" % (name, target or ""))
        yield await Page.attach(connection, found[0]["targetId"])


async def tabs(connection: Connection) -> list[dict[str, Any]]:
    "Every tab, as `Target.getTargets` answers it."
    answer = await connection.send("Target.getTargets")
    return [one for one in answer["targetInfos"] if one["type"] == "page"]


# -- what a tab does ---------------------------------------------------------

#: A ref names a node by its `backendDOMNodeId`, which stays the same
#: across connections, so a ref from one call is good in the next.
REF_PREFIX: Final = "e"

_SKIPPED_ROLES: Final = frozenset({"none", "generic"})
_NO_REF_ROLES: Final = frozenset({"RootWebArea", "StaticText", "InlineTextBox"})
#: What `find_text` climbs past to the element holding a match.
_CLIMBED: Final = _NO_REF_ROLES | {"none"}

#: Keys by their DOM name: the Windows virtual key code Chromium wants,
#: and the text the key types.
KEYS: Final[dict[str, tuple[int, str]]] = {
    "Enter": (13, "\r"),
    "Tab": (9, "\t"),
    "Escape": (27, ""),
    "Backspace": (8, ""),
    "Delete": (46, ""),
    "ArrowUp": (38, ""),
    "ArrowDown": (40, ""),
    "ArrowLeft": (37, ""),
    "ArrowRight": (39, ""),
    "Home": (36, ""),
    "End": (35, ""),
    "PageUp": (33, ""),
    "PageDown": (34, ""),
}

_CENTRE: Final = """function() {
  this.scrollIntoView({block: 'center', inline: 'center'});
  const box = this.getBoundingClientRect();
  return {x: box.x + box.width / 2, y: box.y + box.height / 2};
}"""


async def goto(page: Page, url: str, timeout: float) -> dict[str, Any]:
    "Load `url` and wait for its load event, or `timeout`."
    loaded = anyio.Event()
    await page.send("Page.enable")
    stop = page.listen("Page.loadEventFired", lambda _: loaded.set())
    try:
        answer = await page.send("Page.navigate", {"url": url}, timeout=timeout)
        if answer.get("errorText"):
            raise CDPError("%s: %s" % (url, answer["errorText"]))
        with anyio.move_on_after(timeout):
            await loaded.wait()
    finally:
        stop()
    return {"url": url, "loaded": loaded.is_set(), "title": await evaluate(page, "document.title")}


async def evaluate(page: Page, expression: str, timeout: float = 30.0) -> Any:
    answer = await page.send(
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True, "userGesture": True},
        timeout=timeout,
    )
    if "exceptionDetails" in answer:
        details = answer["exceptionDetails"]
        raise CDPError((details.get("exception") or {}).get("description") or details.get("text", "it threw"))
    return answer.get("result", {}).get("value")


async def snapshot(page: Page, limit: int, within: str | None = None) -> str:
    """
    The page as its accessibility tree: one line a node, indented, with a
    ref on each node an operation can act on. `within` is a ref whose
    subtree alone is answered.
    """
    await page.send("Accessibility.enable")
    nodes = (await page.send("Accessibility.getFullAXTree"))["nodes"]
    by_id = {node["nodeId"]: node for node in nodes}
    lines = ["# %s" % (await evaluate(page, "document.title"),), "# %s" % (await evaluate(page, "location.href"),)]

    def text(node: Mapping[str, Any], key: str) -> str:
        return str((node.get(key) or {}).get("value", ""))

    def walk(node: Mapping[str, Any], depth: int) -> None:
        if len(lines) >= limit:
            return
        role = text(node, "role")
        shown = not node.get("ignored") and role and role not in _SKIPPED_ROLES
        if shown:
            line = "%s- %s" % ("  " * depth, role)
            for key, spelling in (("name", " %s"), ("value", " value=%s")):
                if text(node, key):
                    line += spelling % (json.dumps(text(node, key), ensure_ascii=False),)
            backend = node.get("backendDOMNodeId")
            if backend is not None and role not in _NO_REF_ROLES:
                line += " [ref=%s%d]" % (REF_PREFIX, backend)
            lines.append(line)
        for child in node.get("childIds", ()):
            if child in by_id:
                walk(by_id[child], depth + 1 if shown else depth)

    if within is None:
        roots = [node for node in nodes if not node.get("parentId")]
    else:
        backend = _backend(within)
        roots = [node for node in nodes if node.get("backendDOMNodeId") == backend]
        if not roots:
            raise LookupError("the ref %s is gone, or the accessibility tree has no node for it" % (within,))
    for root in roots:
        walk(root, 0)
    if len(lines) >= limit:
        lines.append("... cut at %d lines" % (limit,))
    return "\n".join(lines)


async def point(page: Page, x: float, y: float, of: Sequence[float] | None) -> tuple[float, float]:
    """
    A point in the viewport's CSS pixels: as given, or read off a picture
    of the viewport `of` = (width, height) pixels, which `fit` and a
    harness shrink.
    """
    if of is None:
        return x, y
    width, height = of
    view = (await page.send("Page.getLayoutMetrics"))["cssVisualViewport"]
    return x * view["clientWidth"] / width, y * view["clientHeight"] / height


async def at(page: Page, x: float, y: float) -> dict[str, Any]:
    """
    What is at a point of the viewport, in CSS pixels: the nearest
    element a person acts on that holds it, else the nearest that the
    accessibility tree names.
    """
    await page.send("DOM.enable")
    try:
        found = await page.send(
            "DOM.getNodeForLocation", {"x": round(x), "y": round(y), "ignorePointerEventsNone": True}
        )
    except CDPError:
        raise LookupError("nothing at %g, %g: it is outside the page" % (x, y)) from None
    holding = await _holding(page, found["backendNodeId"], acted=True)
    if holding is None:
        raise LookupError("nothing the page names at %g, %g" % (x, y))
    return {**holding, "x": x, "y": y}


async def _holding(page: Page, backend: int, acted: bool) -> dict[str, str] | None:
    """
    The ref, role and name of the nearest element the accessibility tree
    names that holds a node: of one a person acts on first, when `acted`.
    """
    await page.send("Accessibility.enable")
    nodes = (await page.send("Accessibility.getPartialAXTree", {"backendNodeId": backend, "fetchRelatives": True}))[
        "nodes"
    ]
    by_id = {node["nodeId"]: node for node in nodes}
    chain = []
    node = next((node for node in nodes if node.get("backendDOMNodeId") == backend), None)
    while node is not None:
        chain.append(node)
        node = by_id.get(node.get("parentId", ""))

    def role(node: Mapping[str, Any]) -> str:
        return str((node.get("role") or {}).get("value", ""))

    named = [
        node
        for node in chain
        if not node.get("ignored") and "backendDOMNodeId" in node and role(node) not in _SKIPPED_ROLES | _NO_REF_ROLES
    ]
    found = ([node for node in named if role(node) in ACTIONABLE] if acted else []) or named
    if not found:
        return None
    return {
        "ref": "%s%d" % (REF_PREFIX, found[0]["backendDOMNodeId"]),
        "role": role(found[0]),
        "name": str((found[0].get("name") or {}).get("value", "")),
    }


#: What the person selected: in a text field, else on the page, and the
#: element around it. Null when nothing is.
_SELECTION: Final = """(() => {
  const field = document.activeElement;
  if (field && typeof field.selectionStart === 'number' && field.selectionEnd > field.selectionStart) {
    return {text: field.value.slice(field.selectionStart, field.selectionEnd), element: field};
  }
  const selected = getSelection();
  if (!selected || selected.isCollapsed) return null;
  let around = selected.getRangeAt(0).commonAncestorContainer;
  if (around.nodeType !== Node.ELEMENT_NODE) around = around.parentElement;
  return {text: selected.toString(), element: around};
})()"""


async def selection(page: Page) -> dict[str, Any] | None:
    """
    What a person selected in the tab, as text, with the element around
    it as a ref; None when nothing is selected.
    """
    answer = await page.send("Runtime.evaluate", {"expression": _SELECTION})
    if "exceptionDetails" in answer or answer["result"].get("subtype") == "null":
        return None
    selected = answer["result"]["objectId"]
    try:
        text = await page.send(
            "Runtime.callFunctionOn",
            {"objectId": selected, "functionDeclaration": "function() { return this.text; }", "returnByValue": True},
        )
        element = await page.send(
            "Runtime.callFunctionOn",
            {"objectId": selected, "functionDeclaration": "function() { return this.element; }"},
        )
        await page.send("DOM.enable")
        described = await page.send("DOM.describeNode", {"objectId": element["result"]["objectId"]})
    finally:
        await page.send("Runtime.releaseObject", {"objectId": selected})
    around = await _holding(page, described["node"]["backendNodeId"], acted=False)
    return {"text": text["result"]["value"], **(around or {})}


def _backend(ref: str) -> int:
    "The `backendDOMNodeId` a ref names."
    if not ref.startswith(REF_PREFIX) or not ref[len(REF_PREFIX) :].isdigit():
        raise ValueError("%r is not a ref; browser.snapshot answers them, as e12" % (ref,))
    return int(ref[len(REF_PREFIX) :])


async def find_text(page: Page, text: str, limit: int) -> list[str]:
    """
    The elements whose name or value holds `text`, any case, one line
    each as `browser.snapshot` writes it, without the rest of the tree.
    A match in running text answers the element that holds the text.
    """
    await page.send("Accessibility.enable")
    nodes = (await page.send("Accessibility.getFullAXTree"))["nodes"]
    by_id = {node["nodeId"]: node for node in nodes}
    wanted = text.casefold()
    lines: list[str] = []
    seen: set[int] = set()

    def value(node: Mapping[str, Any], key: str) -> str:
        return str((node.get(key) or {}).get("value", ""))

    for node in nodes:
        said = value(node, "name") or value(node, "value")
        if node.get("ignored") or wanted not in said.casefold():
            continue
        holder: Mapping[str, Any] | None = node
        # Up from a text run to its element; the page's own node climbs to nothing.
        while holder is not None and (value(holder, "role") in _CLIMBED or "backendDOMNodeId" not in holder):
            holder = by_id.get(holder.get("parentId", ""))
        if holder is None or holder["backendDOMNodeId"] in seen:
            continue
        seen.add(holder["backendDOMNodeId"])
        lines.append(
            "- %s %s [ref=%s%d]"
            % (value(holder, "role"), json.dumps(said, ensure_ascii=False), REF_PREFIX, holder["backendDOMNodeId"])
        )
        if len(lines) >= limit:
            break
    return lines


async def _element(page: Page, ref: str | None, selector: str | None) -> str:
    "The remote object id of the node a ref or a CSS selector names."
    if (ref is None) == (selector is None):
        raise ValueError("name the element by `ref` or by `selector`, one of them")
    if ref is not None:
        backend = _backend(ref)
        await page.send("DOM.enable")
        try:
            resolved = await page.send("DOM.resolveNode", {"backendNodeId": backend})
        except CDPError:
            raise LookupError("the ref %s is gone; take a new browser.snapshot" % (ref,)) from None
        return resolved["object"]["objectId"]
    answer = await page.send("Runtime.evaluate", {"expression": "document.querySelector(%s)" % (json.dumps(selector),)})
    remote = answer.get("result", {})
    if "objectId" not in remote:
        raise LookupError("nothing matches %r" % (selector,))
    return remote["objectId"]


async def _centre(page: Page, element: str) -> tuple[float, float]:
    answer = await page.send(
        "Runtime.callFunctionOn", {"objectId": element, "functionDeclaration": _CENTRE, "returnByValue": True}
    )
    box = answer["result"]["value"]
    return box["x"], box["y"]


async def click(
    page: Page, ref: str | None, selector: str | None, at_point: tuple[float, float] | None = None
) -> dict[str, Any]:
    "Click an element at its centre, or a point of the viewport, and answer what the click hit."
    if at_point is None:
        x, y = await _centre(page, await _element(page, ref, selector))
    elif ref is not None or selector is not None:
        raise ValueError("click an element or a point, not both")
    else:
        x, y = at_point
    # Before the click, which may take the element away.
    try:
        hit = await at(page, x, y)
    except LookupError:
        hit = {"x": x, "y": y}
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
    return hit


async def type_text(page: Page, text: str, ref: str | None, selector: str | None, clear: bool) -> None:
    "Type `text` where the focus is, or into the element named, emptied first when `clear`."
    if ref is not None or selector is not None:
        focus = "function(){ this.focus(); %s }" % ("this.value = '';" if clear else "",)
        await page.send(
            "Runtime.callFunctionOn", {"objectId": await _element(page, ref, selector), "functionDeclaration": focus}
        )
    await page.send("Input.insertText", {"text": text})


async def press(page: Page, key: str) -> None:
    code, text = KEYS.get(key, (ord(key.upper()) if len(key) == 1 else 0, key if len(key) == 1 else ""))
    if not code:
        raise ValueError("no key %r; a character, or one of %s" % (key, ", ".join(KEYS)))
    common = {"key": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
    await page.send("Input.dispatchKeyEvent", {**common, "type": "keyDown", **({"text": text} if text else {})})
    await page.send("Input.dispatchKeyEvent", {**common, "type": "keyUp"})


#: The roles a person acts on: what `marks` labels on a picture.
ACTIONABLE: Final = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "option",
        "listbox",
        "treeitem",
    }
)
#: Mark colours in turn, so neighbours differ; each carries white text.
MARK_COLOURS: Final = ("#d7263d", "#1b6ca8", "#2e8540", "#8e44ad", "#c45500")
LABEL_SIZE: Final = 12


class Mark(NamedTuple):
    "An element a person can act on, and where it is on the page, in CSS pixels."

    ref: str
    role: str
    name: str
    x: float
    y: float
    width: float
    height: float

    def line(self) -> str:
        return "%s %s %s" % (self.ref, self.role, json.dumps(self.name, ensure_ascii=False))


async def marks(page: Page) -> list[Mark]:
    """
    Every element of the top document a person can act on that has a
    box: its role and name from the accessibility tree, and its box from
    one `DOMSnapshot.captureSnapshot`, which holds every box at once.
    An element inside a frame is left out, since its box counts from
    the frame.
    """
    await page.send("Accessibility.enable")
    wanted: dict[int, tuple[str, str]] = {}
    for node in (await page.send("Accessibility.getFullAXTree"))["nodes"]:
        role = str((node.get("role") or {}).get("value", ""))
        if node.get("ignored") or role not in ACTIONABLE or "backendDOMNodeId" not in node:
            continue
        wanted[node["backendDOMNodeId"]] = (role, str((node.get("name") or {}).get("value", "")))
    snapshot = await page.send("DOMSnapshot.captureSnapshot", {"computedStyles": []})
    (top, *_frames) = snapshot["documents"]
    backend = top["nodes"]["backendNodeId"]
    found: dict[int, Mark] = {}
    for index, (x, y, width, height) in zip(top["layout"]["nodeIndex"], top["layout"]["bounds"], strict=True):
        node = backend[index]
        if node in wanted and node not in found and width > 0 and height > 0:
            role, name = wanted[node]
            found[node] = Mark("%s%d" % (REF_PREFIX, node), role, name, x, y, width, height)
    return list(found.values())


def _within(mark: Mark, clip: Mapping[str, float]) -> bool:
    return (
        mark.x < clip["x"] + clip["width"]
        and mark.x + mark.width > clip["x"]
        and mark.y < clip["y"] + clip["height"]
        and mark.y + mark.height > clip["y"]
    )


def draw_marks(png: bytes, clip: Mapping[str, float], found: Sequence[Mark]) -> bytes:
    """
    The picture with each mark boxed and labelled with its ref, at its
    top left. Drawn on the picture and not on the page, so a person
    watching sees no flash, and the page's own scripts see nothing.
    """
    picture = Image.open(io.BytesIO(png)).convert("RGB")
    scale = picture.width / clip["width"]
    pen = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=LABEL_SIZE)
    for number, mark in enumerate(found):
        colour = MARK_COLOURS[number % len(MARK_COLOURS)]
        left, top = (mark.x - clip["x"]) * scale, (mark.y - clip["y"]) * scale
        pen.rectangle((left, top, left + mark.width * scale, top + mark.height * scale), outline=colour, width=2)
        x0, y0, x1, y1 = pen.textbbox((0, 0), mark.ref, font=font)
        at = (max(0.0, left), max(0.0, top - (y1 - y0) - 2))
        pen.rectangle((at[0], at[1], at[0] + x1 - x0 + 4, at[1] + y1 - y0 + 2), fill=colour)
        pen.text((at[0] + 2 - x0, at[1] + 1 - y0), mark.ref, font=font, fill="white")
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


async def screenshot(page: Page, full_page: bool, fit: int | None, marked: bool = False) -> tuple[bytes, list[Mark]]:
    """
    A PNG of the tab, its longest edge `fit` pixels at most, and when
    `marked` each element a person acts on boxed with its ref, which
    the marks answered beside it name.

    The clip's scale shrinks what Chromium draws, so a window on a
    high-density screen answers fewer pixels and not a resample.
    """
    found = await marks(page) if marked else []
    metrics = await page.send("Page.getLayoutMetrics")
    if full_page:
        size = metrics["cssContentSize"]
        clip = {"x": 0, "y": 0, "width": size["width"], "height": size["height"]}
    else:
        view = metrics["cssVisualViewport"]
        clip = {"x": view["pageX"], "y": view["pageY"], "width": view["clientWidth"], "height": view["clientHeight"]}
    density = float(await evaluate(page, "window.devicePixelRatio") or 1)
    longest = max(clip["width"], clip["height"]) * density
    clip["scale"] = min(1.0, fit / longest) if fit and longest else 1.0
    answer = await page.send(
        "Page.captureScreenshot",
        {"format": "png", "clip": clip, "captureBeyondViewport": full_page},
        timeout=60.0,
    )
    png = base64.b64decode(answer["data"])
    if not marked:
        return png, []
    found = [mark for mark in found if _within(mark, clip)]
    return draw_marks(png, clip, found), found


async def console(page: Page, limit: int) -> list[dict[str, Any]]:
    "What the page logged, oldest first: the browser replays it to each new listener."
    said: list[dict[str, Any]] = []

    def logged(params: dict[str, Any]) -> None:
        words = [str(one.get("value", one.get("description", one.get("type", "")))) for one in params["args"]]
        said.append({"type": params["type"], "text": " ".join(words)})

    def threw(params: dict[str, Any]) -> None:
        details = params["exceptionDetails"]
        said.append({"type": "exception", "text": (details.get("exception") or {}).get("description", details["text"])})

    stops = [page.listen("Runtime.consoleAPICalled", logged), page.listen("Runtime.exceptionThrown", threw)]
    try:
        await page.send("Runtime.enable")
        # The replay follows the answer to `Runtime.enable`; a round trip
        # after it is behind every replayed event.
        await page.send("Runtime.evaluate", {"expression": "0"})
    finally:
        for stop in stops:
            stop()
    return said[-limit:]
