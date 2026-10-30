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
import json
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import anyio
from libpymux import Job, Pane, Server, Window

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
            port, path = (self.profile / "DevToolsActivePort").read_text().split()
        except OSError, ValueError:
            return None
        return "ws://127.0.0.1:%s%s" % (port, path)


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
) -> Browser:
    "Start a browser in `seat`, and answer it once it listens: in a split of `window` when one is named."
    if window is not None and seat is not Seat.PANE:
        raise ValueError("only a `pane` browser opens in a window")
    for existing in await every(server):
        if existing.name == name:
            raise ValueError("a browser named %r is open; close it or pick another name" % (name,))
    profile = Path(tempfile.mkdtemp(prefix="pymux-browser-"))
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
        shutil.rmtree(profile, ignore_errors=True)
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
    Stop the browser, and take its holder and its profile away.

    Chromium is asked to quit first. Killing a window pane closes one of
    its windows and nothing more, and the next one opens in its place.
    """
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
    finally:
        shutil.rmtree(browser.profile, ignore_errors=True)


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


async def snapshot(page: Page, limit: int) -> str:
    """
    The page as its accessibility tree: one line a node, indented, with a
    ref on each node an operation can act on.
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

    roots = [node for node in nodes if not node.get("parentId")]
    for root in roots:
        walk(root, 0)
    if len(lines) >= limit:
        lines.append("... cut at %d lines" % (limit,))
    return "\n".join(lines)


async def _element(page: Page, ref: str | None, selector: str | None) -> str:
    "The remote object id of the node a ref or a CSS selector names."
    if (ref is None) == (selector is None):
        raise ValueError("name the element by `ref` or by `selector`, one of them")
    if ref is not None:
        if not ref.startswith(REF_PREFIX) or not ref[len(REF_PREFIX) :].isdigit():
            raise ValueError("%r is not a ref; browser.snapshot answers them, as e12" % (ref,))
        await page.send("DOM.enable")
        try:
            resolved = await page.send("DOM.resolveNode", {"backendNodeId": int(ref[len(REF_PREFIX) :])})
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


async def click(page: Page, ref: str | None, selector: str | None) -> dict[str, float]:
    x, y = await _centre(page, await _element(page, ref, selector))
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
    return {"x": x, "y": y}


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


async def screenshot(page: Page, full_page: bool, fit: int | None) -> bytes:
    """
    A PNG of the tab, its longest edge `fit` pixels at most.

    The clip's scale shrinks what Chromium draws, so a window on a
    high-density screen answers fewer pixels and not a resample.
    """
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
    return base64.b64decode(answer["data"])


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
