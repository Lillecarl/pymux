"""
A click lands where the agent aimed it, at any device pixel ratio.

The page is the judge: its own listener logs each click's target and
point, and `getBoundingClientRect` says where each target is. Nothing
here takes a position from pymux-mcp, so a wrong space in pymux-mcp
cannot agree with itself. Lillecarl/pyterm#997.

Runs in `checks.pymux-browsers`.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image, ImageChops, ImageColor
from pymux_mcp import browser
from test_mcp_browsers import ARGUMENTS, call, ref, server  # noqa: F401  (server is a fixture)

#: Small and far apart: a scale error of a few percent misses a 20 px
#: target far from the origin, where one near it still hits.
TARGETS = {"nw": "#00ff00", "mid": "#ff00ff", "deep": "#00ffff", "pinned": "#ffff00"}
PLACES = {
    "nw": "left:40px;top:40px",
    "mid": "left:55%;top:300px",
    "deep": "left:300px;top:1500px",
    "pinned": "position:fixed;right:40px;bottom:40px",
}
SCROLL = 1200
PAGE = (
    "data:text/html;base64,"
    + base64.b64encode(
        (
            # Without a doctype, quirks mode answers the document's height as
            # the viewport's.
            "<!doctype html><title>aim</title><style>body{margin:0;height:2400px;position:relative;background:#fff}"
            "button{position:absolute;width:20px;height:20px;padding:0;border:0;min-width:0}</style>"
            + "".join(
                "<button id=%s aria-label=%s style='%s;background:%s'></button>" % (name, name, PLACES[name], colour)
                for name, colour in TARGETS.items()
            )
            + "<script>window.hits = [];"
            " addEventListener('click', e => hits.push({id: e.target.id, x: e.clientX, y: e.clientY}), true);</script>"
        ).encode()
    ).decode()
)

#: Where each target is in the viewport, in CSS pixels, the scroll, and
#: the clicks logged since the last look, which it takes away. The
#: viewport's size leaves the scrollbar out, as the picture does;
#: `innerWidth` counts it.
_RECTS = """JSON.stringify({
  ratio: devicePixelRatio, scrollX, scrollY, hits: hits.splice(0),
  width: document.documentElement.clientWidth, height: document.documentElement.clientHeight,
  rects: Object.fromEntries([...document.querySelectorAll('button')].map(b => {
    const r = b.getBoundingClientRect(); return [b.id, [r.x, r.y, r.width, r.height]];
  })),
})"""


async def _view(server) -> dict:
    return json.loads(await call(server, "browser.evaluate", name="aim", expression=_RECTS))


async def _scroll(server, y: int) -> dict:
    expression = "scrollTo(0, %d), %s" % (y, _RECTS)
    return json.loads(await call(server, "browser.evaluate", name="aim", expression=expression))


def _visible(rect: list[float], view: dict) -> bool:
    x, y, width, height = rect
    return x >= 0 and y >= 0 and x + width <= view["width"] and y + height <= view["height"]


def _drawn_at(picture: Image.Image, colour: str) -> tuple[float, float] | None:
    "The centre of what the picture draws in `colour`, in picture pixels."
    plain = Image.new("RGB", picture.size, ImageColor.getrgb(colour))
    # The largest channel's difference: luminance weighs blue at a tenth,
    # so white reads as near yellow.
    red, green, blue = ImageChops.difference(picture, plain).split()
    farthest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    box = farthest.point(lambda value: 255 if value < 40 else 0).getbbox()
    return None if box is None else ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


async def _refs(server) -> dict[str, str]:
    snapshot = await call(server, "browser.snapshot", name="aim")
    return {
        name: ref(next(line for line in snapshot.splitlines() if 'button "%s"' % (name,) in line)) for name in TARGETS
    }


async def _judge(server, misses: list[str], route: str, target: str, hit: dict, refs: dict[str, str]) -> None:
    "What the page logged against what was aimed at, and what pymux-mcp said it hit."
    view = await _view(server)
    logged = view["hits"]
    x, y, width, height = view["rects"][target]
    if [one["id"] for one in logged] != [target]:
        misses.append("%s at %s: the page logged %s" % (route, target, logged))
    elif not (x <= logged[0]["x"] <= x + width and y <= logged[0]["y"] <= y + height):
        misses.append("%s at %s: landed at %s, outside %s" % (route, target, logged[0], view["rects"][target]))
    if hit.get("ref") != refs[target]:
        misses.append("%s at %s: answered %s, not %s" % (route, target, hit, refs[target]))


#: Default `fit`, and one far smaller than the page.
FITS = (1568, 300)
#: Every route at every ratio: a browser in a pane answers each
#: operation about ten times slower, 183s against 17s measured, so a
#: pane takes one scroll and one `fit`. Lillecarl/pyterm#999.
EVERY = ((0, SCROLL), FITS)
FEWER = ((0,), FITS[:1])


async def _aim(server, misses: list[str], refs: dict[str, str], scroll: int, fits: tuple[int, ...]) -> None:
    """
    Each route an agent aims by: a point in CSS pixels, a point read off
    a picture at each `fit`, a ref and a selector.
    """
    where = "scrolled %d" % (scroll,)
    view = await _scroll(server, scroll)
    shown = [name for name, rect in view["rects"].items() if _visible(rect, view)]
    for target in shown:
        x, y, width, height = view["rects"][target]
        hit = await call(server, "browser.click", name="aim", x=x + width / 2, y=y + height / 2)
        await _judge(server, misses, "%s, css point" % (where,), target, hit, refs)
    # A click at a point scrolls nothing, so only a ref or a selector
    # needs the scroll put back.
    for fit in fits:
        shot = await call(server, "browser.screenshot", name="aim", fit=fit)
        picture = Image.open(io.BytesIO(shot.data)).convert("RGB")
        for target in shown:
            route = "%s, picture fit=%s %dx%d" % (where, fit, *picture.size)
            centre = _drawn_at(picture, TARGETS[target])
            if centre is None:
                misses.append("%s: %s is not in the picture" % (route, target))
                continue
            hit = await call(server, "browser.click", name="aim", x=centre[0], y=centre[1], of=list(picture.size))
            await _judge(server, misses, route, target, hit, refs)
    for target in TARGETS:
        for by in ("ref", "selector"):
            await _scroll(server, scroll)
            named = {"ref": refs[target]} if by == "ref" else {"selector": "#" + target}
            hit = await call(server, "browser.click", name="aim", **named)
            await _judge(server, misses, "%s, %s" % (where, by), target, hit, refs)


async def _mark(server, misses: list[str], refs: dict[str, str], scroll: int, fits: tuple[int, ...]) -> None:
    """
    A mark's box is its element's box, in CSS pixels of the document,
    and the marked picture draws it there.
    """
    where = "scrolled %d" % (scroll,)
    view = await _scroll(server, scroll)
    async with browser.tab(server, "aim") as page:
        found = {mark.ref: mark for mark in await browser.marks(page)}
    for target, (x, y, width, height) in view["rects"].items():
        mark = found.get(refs[target])
        want = (x + view["scrollX"], y + view["scrollY"], width, height)
        if mark is None:
            misses.append("%s: no mark for %s" % (where, target))
        elif max(abs(a - b) for a, b in zip((mark.x, mark.y, mark.width, mark.height), want, strict=True)) > 1:
            misses.append("%s: %s marked at %s, is at %s" % (where, target, tuple(mark[3:]), want))
    colours = {ImageColor.getrgb(colour) for colour in browser.MARK_COLOURS}
    for fit in fits:
        shot = await call(server, "browser.screenshot", name="aim", marks=True, fit=fit)
        picture = Image.open(io.BytesIO(shot.data)).convert("RGB")
        scale = picture.width / view["width"]
        route = "%s, marked picture fit=%s %dx%d" % (where, fit, *picture.size)
        for target in (name for name, rect in view["rects"].items() if _visible(rect, view)):
            x, y, width, height = view["rects"][target]
            if not any(line.startswith(refs[target] + " ") for line in shot.caption.splitlines()):
                misses.append("%s: %s is not in the caption" % (route, target))
                continue
            # The box's left edge, half way down; the pen is two pixels wide.
            edge = (round(x * scale), round((y + height / 2) * scale))
            near = [picture.getpixel((edge[0] + dx, edge[1])) for dx in (-1, 0, 1)]
            if not colours & set(near):
                misses.append("%s: no mark on %s's left edge %s, found %s" % (route, target, edge, near))


@pytest.mark.parametrize(
    ("seat", "ratio", "routes"),
    [("headless", 1.0, EVERY), ("headless", 1.125, EVERY), ("headless", 2.0, EVERY), ("pane", 1.125, FEWER)],
    ids=["headless-1", "headless-1.125", "headless-2", "pane-1.125"],
)
async def test_a_click_lands_where_it_was_aimed(server, seat, ratio, routes):
    """
    At the top of the page and scrolled, where a document point and a
    viewport point part. One browser for every route, since a launch
    costs more than all of them.
    """
    arguments = [*ARGUMENTS, "--force-device-scale-factor=%g" % (ratio,)]
    await call(server, "browser.open", name="aim", seat=seat, url=PAGE, arguments=arguments)
    try:
        view = await _view(server)
        # Else every case below runs at a ratio of 1 and tests nothing new.
        assert view["ratio"] == ratio, view
        refs = await _refs(server)
        misses: list[str] = []
        scrolls, fits = routes
        for scroll in scrolls:
            await _mark(server, misses, refs, scroll, fits)
            await _aim(server, misses, refs, scroll, fits)
        assert not misses, "\n".join(misses)
    finally:
        await call(server, "browser.close", name="aim")
