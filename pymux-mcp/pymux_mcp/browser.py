"""
Chromium for an agent to drive, over the DevTools protocol, where a
person can watch it. Lillecarl/pymux#822.

A browser runs in one of two seats:

- `headless`: a job of the server, with nothing to see.
- `pane`: a window pane of the server, through `wl`. Anybody attached
  watches it, over SSH too.

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
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import anyio
from libpymux import Job, Server, Window

from .cdp import CDPError, Connection, Page, connected

__all__ = ["Browser", "Seat", "close", "find", "open_browser", "tab"]

#: The tag a browser's job or pane carries, with the browser's name.
NAME_TAG: Final = "browser"
#: The tag holding the profile, where the port is written.
PROFILE_TAG: Final = "browser-profile"
SEAT_TAG: Final = "browser-seat"

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


class Seat(StrEnum):
    HEADLESS = "headless"
    PANE = "pane"


class BrowserGone(LookupError):
    "No browser by that name, or one that stopped listening."


class Browser:
    """
    A browser that pymux holds, and its profile.

    A `pane` browser's holder is its window and not its pane: a pane
    shows one window of Chromium, and Chromium opens the next one beside
    it in the same window.
    """

    def __init__(self, name: str, seat: Seat, profile: Path, holder: Job | Window) -> None:
        self.name = name
        self.seat = seat
        self.profile = profile
        self.holder = holder

    def json(self) -> dict[str, Any]:
        found: dict[str, Any] = {"name": self.name, "seat": self.seat.value, "profile": str(self.profile)}
        if isinstance(self.holder, Job):
            found["job"] = self.holder.id
        else:
            found["window"] = self.holder.id
        return found

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
    wanted = {NAME_TAG: name}
    holders: list[Job | Window] = [job for job in await server.jobs(wanted) if job.running]
    holders += await server.windows(wanted)
    if not holders:
        raise BrowserGone("no browser named %r; browser.open makes one, browser.list shows them" % (name,))
    return await _browser(server, holders[-1])


async def every(server: Server) -> list[Browser]:
    holders: list[Job | Window] = [job for job in await server.jobs({NAME_TAG: None}) if job.running]
    holders += await server.windows({NAME_TAG: None})
    return [await _browser(server, holder) for holder in holders]


async def _browser(server: Server, holder: Job | Window) -> Browser:
    tags = holder.tags if isinstance(holder, Job) else (await server.tags()).get(holder.id, {})
    return Browser(tags[NAME_TAG] or "", Seat(tags[SEAT_TAG]), Path(tags[PROFILE_TAG] or ""), holder)


async def open_browser(
    server: Server,
    name: str,
    seat: Seat,
    url: str,
    session: str | None,
    executable: str | None,
    arguments: Sequence[str],
) -> Browser:
    "Start a browser in `seat`, and answer it once it listens."
    for existing in await every(server):
        if existing.name == name:
            raise ValueError("a browser named %r is open; close it or pick another name" % (name,))
    profile = Path(tempfile.mkdtemp(prefix="pymux-browser-"))
    tags = {NAME_TAG: name, SEAT_TAG: seat.value, PROFILE_TAG: str(profile)}
    argv = [
        "--remote-debugging-port=0",
        "--user-data-dir=%s" % (profile,),
        *QUIET,
        *arguments,
    ]
    holder: Job | Window
    try:
        if seat is Seat.HEADLESS:
            command = [chromium(executable), "--headless=new", *argv, url]
            holder = await server.run_job(shlex.join(command), tags=tags)
        else:
            command = [chromium(executable), "--ozone-platform=wayland", *argv, url]
            holder = await _in_a_window(server, name, session, command, tags)
        browser = Browser(name, seat, profile, holder)
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
    # `wl` answers once the window opened, or with what the program said
    # when it ended first.
    result = await server.cmd(["wl", "-t", pane.id, "--", *command], check=False)
    if result.exit_code != 0:
        await window.kill()
        raise CDPError("chromium ended before it opened a window: %s" % ((result.stdout + result.stderr).strip(),))
    return window


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
        if await _held(browser):
            await browser.holder.kill()
    finally:
        shutil.rmtree(browser.profile, ignore_errors=True)


async def _held(browser: Browser) -> bool:
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


async def screenshot(page: Page, full_page: bool) -> bytes:
    answer = await page.send(
        "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full_page}, timeout=60.0
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
