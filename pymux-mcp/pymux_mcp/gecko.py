"""
What a tab of Firefox or Glide does, over `rdp`: the operations
`browser.py` holds for Chromium, by the same names, for the browsers that
speak no DevTools protocol of Chromium's.

Work happens in two places, both with full privilege:

- The parent process, whose console evaluates in the browser window:
  tabs, loading an address, pictures (`drawSnapshot`), quitting, and
  `chrome`.
- The content process that holds the tab, through `gecko_page.js`, which
  reads the accessibility tree, keeps refs, and synthesizes input with
  Marionette's EventUtils, so a page sees a click as trusted.

Neither sets `navigator.webdriver`, which Marionette and WebDriver BiDi
do, and which a site that refuses robots reads.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final

from libpymux import Server

from .browser import ACTIONABLE, KEYS, MARK_COLOURS, Browser, BrowserGone, Mark, draw_marks, find, touch
from .rdp import Connection, RDPError, connected

__all__ = ["Tab", "chrome", "listed", "quit_browser", "tab"]

PAGE_SOURCE: Final = resources.files(__package__).joinpath("gecko_page.js").read_text()
#: A content process keeps the library under this version, and loads it
#: again when the source changed.
PAGE_VERSION: Final = hashlib.sha256(PAGE_SOURCE.encode()).hexdigest()[:16]

#: How long a picture may take: a whole page is large.
PICTURE_TIMEOUT: Final = 60.0


@dataclass
class Tab:
    "One tab, and the console actors that reach its browser window and its content process."

    connection: Connection
    window: str
    content: str | None
    browser_id: int
    context: int

    async def page(self, name: str, **args: Any) -> Any:
        "Call `name` of `gecko_page.js` in the tab's content process, and answer its value."
        if self.content is None:
            raise BrowserGone("the tab is not loaded; browser.goto loads it")
        text = _CALL % {
            "version": json.dumps(PAGE_VERSION),
            "source": json.dumps(PAGE_SOURCE),
            "name": json.dumps(name),
            "context": self.context,
            "args": json.dumps(args),
        }
        return json.loads(await _value(self.connection, self.content, text))

    async def chrome(self, text: str, timeout: float = 30.0) -> Any:
        "Evaluate `text` in the browser window, which answers JSON text."
        return json.loads(await _value(self.connection, self.window, text, timeout))


#: Runs an expression that may answer a promise, and answers JSON with its
#: value or its error: a rejected promise reaches the client with no reason.
_CAUGHT: Final = """(async () => {
  try {
    return JSON.stringify({value: await (%s)});
  } catch (error) {
    return JSON.stringify({error: String(error?.message ?? error)});
  }
})()"""


async def _value(connection: Connection, console: str, text: str, timeout: float = 30.0) -> Any:
    "The value of the expression `text`, which answers JSON text, in `console`."
    answer = json.loads(await connection.evaluate(console, _CAUGHT % (text,), timeout))
    if "error" in answer:
        raise LookupError(answer["error"])
    return answer["value"]


#: Loads the library into a sandbox of the content process once a version,
#: and calls into it.
_CALL: Final = """(async () => {
  const shared = Cu.getGlobalForObject(Services);
  let library = shared.__pymuxGecko;
  if (library?.version !== %(version)s) {
    const sandbox = Cu.Sandbox(Services.scriptSecurityManager.getSystemPrincipal(), {sandboxName: "pymux-mcp"});
    Object.assign(sandbox, {
      Cc, Ci, Cu, Services,
      BrowsingContext: shared.BrowsingContext,
      WindowGlobalChild: shared.WindowGlobalChild,
    });
    Cu.evalInSandbox(%(source)s, sandbox, "latest", "resource://pymux-mcp/gecko_page.js", 1);
    library = shared.__pymuxGecko = {version: %(version)s, call: sandbox.call};
  }
  return await library.call(%(name)s, %(context)d, %(args)s);
})()"""

#: How long a browser that has no ready window yet is waited for.
READY_WITHIN_MS: Final = 10_000

#: Every tab of every browser window, the selected one of the front window
#: first.
#:
#: **A window is listed before it has a `gBrowser`.** Firefox registers a
#: `navigator:browser` window, then sets `gBrowser` in its startup, and
#: `browser.open` asked in between: "front.gBrowser is undefined", now
#: and then, under load. Lillecarl/pyterm#947. So it waits while no
#: window is ready, and leaves out one that is not.
_TABS: Final = """(async () => {
  const ready = () => [...Services.wm.getEnumerator("navigator:browser")].filter(win => win.gBrowser);
  const deadline = Date.now() + %(ms)d;
  while (!ready().length && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
  const front = Services.wm.getMostRecentWindow("navigator:browser");
  const tabs = front?.gBrowser ? [front.gBrowser.selectedTab] : [];
  for (const win of ready()) {
    for (const tab of win.gBrowser.tabs) if (!tabs.includes(tab)) tabs.push(tab);
  }
  return JSON.stringify(tabs.map(tab => {
    const browser = tab.linkedBrowser;
    return {
      browserId: browser.browserId,
      context: browser.browsingContext?.id ?? null,
      pid: browser.browsingContext?.currentWindowGlobal?.osPid ?? null,
      title: tab.label,
      url: browser.currentURI?.spec ?? "",
    };
  }));
})()""" % {"ms": READY_WITHIN_MS}

#: The `<browser>` of a tab by its browser id, `%(browser)d`, and the
#: `gBrowser` of its window. A window with none yet holds no tab (`_TABS`).
_BROWSER: Final = """const [gBrowser, browser] = (() => {
    for (const win of Services.wm.getEnumerator("navigator:browser")) {
      const found = win.gBrowser?.browsers.find(one => one.browserId === %(browser)d);
      if (found) return [win.gBrowser, found];
    }
    throw new Error("the tab is gone");
  })();"""

_GOTO: Final = (
    """(async () => {
  """
    + _BROWSER
    + """
  const STOP = Ci.nsIWebProgressListener.STATE_STOP | Ci.nsIWebProgressListener.STATE_IS_WINDOW;
  let listener;
  const stopped = new Promise(resolve => {
    listener = {
      onStateChange(changed, progress, request, flags, status) {
        if (changed === browser && progress.isTopLevel && (flags & STOP) === STOP) resolve(status);
      },
    };
    gBrowser.addTabsProgressListener(listener);
  });
  try {
    browser.fixupAndLoadURIString(%(url)s, {
      triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
    });
    const status = await Promise.race([stopped, new Promise(resolve => setTimeout(() => resolve(null), %(ms)d))]);
    const failed = status ? Object.keys(Cr).find(name => Cr[name] === status) ?? String(status) : null;
    return JSON.stringify({
      url: browser.currentURI.spec, loaded: status !== null, failed, title: browser.contentTitle,
    });
  } finally {
    gBrowser.removeTabsProgressListener(listener);
  }
})()"""
)

#: A picture of `rect` in page pixels, as base64 PNG: as DevTools draws
#: one, from the top of the page for a whole page.
_PICTURE: Final = """(async () => {
  const context = BrowsingContext.get(%(context)d);
  const bitmap = await context.currentWindowGlobal.drawSnapshot(
    new DOMRect(%(x)f, %(y)f, %(width)f, %(height)f), %(scale)f, "white", {resetScrollPosition: %(whole)s});
  const canvas = document.createElementNS("http://www.w3.org/1999/xhtml", "canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d").drawImage(bitmap, 0, 0);
  bitmap.close();
  return JSON.stringify(canvas.toDataURL("image/png").split(",")[1]);
})()"""

#: Runs code for `chrome` in a sandbox over the browser window, with
#: `glide`, Glide's config API, in scope, and answers the value of its
#: last statement, a promise awaited. Not `eval`, which the window's CSP
#: forbids.
_CHROME: Final = """(async () => {
  const sandbox = Cu.Sandbox(window, {sandboxPrototype: window, wantXrays: false, sandboxName: "pymux-mcp chrome"});
  sandbox.glide = globalThis.GlideBrowser?.api;
  let value = await Cu.evalInSandbox(%(code)s, sandbox, "latest", "resource://pymux-mcp/chrome.js", 1);
  try {
    return JSON.stringify({value: value === undefined ? null : value}) ?? '{"value": null}';
  } catch (error) {
    return JSON.stringify({value: String(value)});
  }
})()"""


async def _window_console(connection: Connection) -> str:
    "The console of the parent process, which evaluates in the browser window."
    process = await connection.send("root", "getProcess", id=0)
    target = await connection.send(process["processDescriptor"]["actor"], "getTarget")
    return target["process"]["consoleActor"]


async def _content_console(connection: Connection, pid: int) -> str:
    "The console of the content process `pid`, which evaluates with the system's privilege."
    for process in (await connection.send("root", "listProcesses"))["processes"]:
        if process["id"] == pid and not process["isParent"]:
            target = await connection.send(process["actor"], "getTarget")
            return target["process"]["consoleActor"]
    raise BrowserGone("no content process %d" % (pid,))


def socket_of(found: Browser) -> Path:
    path = found.endpoint()
    if path is None:
        raise BrowserGone("the browser %r is not listening" % (found.name,))
    return Path(path)


@asynccontextmanager
async def tab(server: Server, name: str, target: str | None = None) -> AsyncIterator[Tab]:
    "A tab of the browser called `name`, for the block: `target`, or the one in front."
    found = await find(server, name)
    path = socket_of(found)
    await touch(server, found)
    async with connected(path) as connection:
        window = await _window_console(connection)
        tabs = json.loads(await _value(connection, window, _TABS))
        if target is not None:
            tabs = [one for one in tabs if str(one["browserId"]) == target]
        if not tabs:
            raise BrowserGone("the browser %r has no tab %s" % (name, target or ""))
        chosen = tabs[0]
        content = None if chosen["pid"] is None else await _content_console(connection, chosen["pid"])
        yield Tab(connection, window, content, chosen["browserId"], chosen["context"] or 0)


async def listed(path: Path) -> list[dict[str, Any]]:
    "Every tab, as `browser.tabs` answers it."
    async with connected(path) as connection:
        window = await _window_console(connection)
        tabs = json.loads(await _value(connection, window, _TABS))
    return [{"tab": str(one["browserId"]), "title": one["title"], "url": one["url"]} for one in tabs]


async def quit_browser(path: Path) -> None:
    "Ask the browser to quit; its connection ends before it can answer."
    async with connected(path) as connection:
        window = await _window_console(connection)
        await connection.send(window, "evaluateJSAsync", text="Services.startup.quit(Ci.nsIAppStartup.eForceQuit)")


async def chrome(path: Path, code: str, timeout: float) -> Any:
    "Run `code` in the browser window, with `glide` in scope in Glide, and answer its value."
    async with connected(path) as connection:
        window = await _window_console(connection)
        answer = json.loads(await _value(connection, window, _CHROME % {"code": json.dumps(code)}, timeout))
    return answer["value"]


# -- what a tab does, by the names browser.py gives Chromium's ---------------


async def goto(page: Tab, url: str, timeout: float) -> dict[str, Any]:
    "Load `url` and wait until it stopped loading, or `timeout`."
    answer = await page.chrome(
        _GOTO % {"browser": page.browser_id, "url": json.dumps(url), "ms": int(timeout * 1000)}, timeout + 5
    )
    if answer["failed"]:
        raise RDPError("%s: %s" % (url, answer["failed"]))
    return {"url": answer["url"], "loaded": answer["loaded"], "title": answer["title"]}


async def evaluate(page: Tab, expression: str, timeout: float = 30.0) -> Any:
    del timeout
    return await page.page("evaluate", expression=expression)


async def snapshot(page: Tab, limit: int, within: str | None = None) -> str:
    return await page.page("snapshot", limit=limit, within=within)


async def find_text(page: Tab, text: str, limit: int) -> list[str]:
    return await page.page("find", text=text, limit=limit)


async def point(page: Tab, x: float, y: float, of: Sequence[float] | None) -> tuple[float, float]:
    if of is None:
        return x, y
    width, height = of
    view = await page.page("metrics")
    return x * view["width"] / width, y * view["height"] / height


async def at(page: Tab, x: float, y: float) -> dict[str, Any]:
    found = await page.page("at", x=x, y=y, actionable=sorted(ACTIONABLE))
    if found is None:
        raise LookupError("nothing the page names at %g, %g" % (x, y))
    return {**found, "x": x, "y": y}


async def click(
    page: Tab, ref: str | None, selector: str | None, at_point: tuple[float, float] | None = None
) -> dict[str, Any]:
    if at_point is None:
        centre = await page.page("centre", ref=ref, selector=selector)
        x, y = centre["x"], centre["y"]
    elif ref is not None or selector is not None:
        raise ValueError("click an element or a point, not both")
    else:
        x, y = at_point
    try:
        hit = await at(page, x, y)
    except LookupError:
        hit = {"x": x, "y": y}
    await page.page("click", x=x, y=y)
    return hit


async def point_at(page: Tab, ref: str | None, selector: str | None, label: str, seconds: float) -> None:
    await page.page("point", ref=ref, selector=selector, label=label, seconds=seconds, colour=MARK_COLOURS[0])


async def type_text(page: Tab, text: str, ref: str | None, selector: str | None, clear: bool) -> None:
    await page.page("type", text=text, ref=ref, selector=selector, clear=clear)


async def press(page: Tab, key: str) -> None:
    if len(key) != 1 and key not in KEYS:
        raise ValueError("no key %r; a character, or one of %s" % (key, ", ".join(KEYS)))
    await page.page("press", key=key)


async def selection(page: Tab) -> dict[str, Any] | None:
    return await page.page("selection")


async def screenshot(page: Tab, full_page: bool, fit: int | None, marked: bool = False) -> tuple[bytes, list[Mark]]:
    view = await page.page("metrics")
    if full_page:
        clip = {"x": 0.0, "y": 0.0, "width": view["pageWidth"], "height": view["pageHeight"]}
    else:
        clip = {"x": view["x"], "y": view["y"], "width": view["width"], "height": view["height"]}
    longest = max(clip["width"], clip["height"]) * view["density"]
    scale = view["density"] * (min(1.0, fit / longest) if fit and longest else 1.0)
    data = await page.chrome(
        _PICTURE % {"context": page.context, **clip, "scale": scale, "whole": json.dumps(full_page)},
        PICTURE_TIMEOUT,
    )
    png = base64.b64decode(data)
    if not marked:
        return png, []
    found = [Mark(**one) for one in await page.page("marks", clip=clip, actionable=sorted(ACTIONABLE))]
    return draw_marks(png, clip, found), found


async def console(page: Tab, limit: int) -> list[dict[str, Any]]:
    return await page.page("console", limit=limit)
