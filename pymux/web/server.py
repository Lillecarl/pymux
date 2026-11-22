"""
`pymux web`: a pane in a browser, for somebody with no front end of their own.

**This is an adapter and not a server.** It runs beside a pymux server, not
inside it: one `libpymux.PaneStream` per viewer over the socket, and the
frames copied to a websocket. So the server process never imports a web
library, which is the whole of what "optional" means here, and this is the
same relay path a caller with its own front end uses -- proving it works
rather than claiming it.

**Loopback and a token, both.** A browser sends no credentials on a
websocket upgrade, so on loopback an `Origin` check is the only thing
between a page somebody happens to have open and their shell, and it is a
weak one: a page may simply omit the header. So every upgrade carries a
token that this prints, kept in a file between runs. `--bind` widens it and says what that costs.

It needs `pymux[web]`. Lillecarl/pyterm#463.
"""

from __future__ import annotations

import functools
import json
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import resvg_py
from libpymux import PaneStream, Server, StreamRefused
from platformdirs import user_state_path

from pymux.log import logger

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response

__all__ = ["MISSING", "a_token", "kept_token", "serve", "token_file"]

#: What to say when the extra is not installed. Named here so the command
#: and this module cannot drift about it.
MISSING = (
    "pymux web needs the web extra: install pymux[web], or use `stream-pane` over the socket, which needs nothing."
)

#: What is served. Two things fill it: `pyproject.toml` declares the files
#: a person wrote, and the build compiles `web/client/` into the three the
#: compiler writes.
STATIC = Path(__file__).parent / "static"

#: What a token is worth guessing. 32 bytes of urlsafe base64 is 43
#: characters; a token in a URL is copied by hand, and this is the point
#: past which shorter would be a choice about typing rather than about
#: security.
TOKEN_BYTES = 32

#: The path of the whole session, as one client of it sees it. `/pane/<id>`
#: is one pane's own screen; this is what a client draws over and around
#: the panes. Lillecarl/pyterm#483.
SESSION = "/session"

#: What may be served, by name. A list and not a directory walk: a path
#: this does not name is not served, so no traversal can reach anything.
SERVED = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/page.js": ("page.js", "text/javascript; charset=utf-8"),
    "/page.css": ("page.css", "text/css; charset=utf-8"),
    "/pymux-pane.js": ("pymux-pane.js", "text/javascript; charset=utf-8"),
    "/pymux-pane.d.ts": ("pymux-pane.d.ts", "text/plain; charset=utf-8"),
    # The element imports this one, so a browser asks for it by itself.
    # A name missing from this table is a 404, and a 404 here means the
    # element never loads at all.
    "/keys.js": ("keys.js", "text/javascript; charset=utf-8"),
    "/keys.d.ts": ("keys.d.ts", "text/plain; charset=utf-8"),
    "/retry.js": ("retry.js", "text/javascript; charset=utf-8"),
    "/retry.d.ts": ("retry.d.ts", "text/plain; charset=utf-8"),
    # The player of `record-export -F web`. Lillecarl/pyterm#612.
    "/replay.html": ("replay.html", "text/html; charset=utf-8"),
    "/replay.js": ("replay.js", "text/javascript; charset=utf-8"),
    "/replay.d.ts": ("replay.d.ts", "text/plain; charset=utf-8"),
    "/replay-page.js": ("replay-page.js", "text/javascript; charset=utf-8"),
    # What a phone needs to put the page on its home screen and open it
    # without the address bar. **The manifest names no `start_url`**, so
    # the address added is the one open, token and all: a fixed one
    # would open a page with no way in.
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    # At the root, so its scope is the whole page: it shows the
    # notifications of a session view. Lillecarl/pyterm#1073.
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    # The fonts a package built with them serves, `nix/web-fonts.nix`;
    # without them it is empty, and the page draws with the device's.
    "/fonts.css": ("fonts.css", "text/css; charset=utf-8"),
}

#: The font files `fonts.css` names. Each name carries a hash of its
#: content, so the browser may keep one: it holds no token, and a phone
#: should not fetch a megabyte of font on every load. Lillecarl/pyterm#1070.
FONT = re.compile(r"/fonts/([A-Za-z0-9-]+\.[0-9a-f]+\.woff2)")
KEPT = {"Cache-Control": "public, max-age=31536000, immutable"}

#: The icon at the sizes a launcher asks for, rendered from `icon.svg`:
#: Chrome on Android installs an app only with a 192 and a 512 pixel PNG.
ICON_SIZES = {"/icon-192.png": 192, "/icon-512.png": 512}


#: On every answer this gives over HTTP. The page loads the element and
#: nothing else, and the element needs no `unsafe-inline` of any kind:
#: its rules go through the CSSOM. No other site may frame the page, which
#: would put a writable terminal under somebody else's clicks.
GUARDS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The address carries the token, so nothing between keeps a copy.
    "Cache-Control": "no-store",
}


def same_origin(origin: str | None, host: str | None) -> bool:
    """
    Whether a websocket upgrade came from this server's own page.

    **A browser always sends `Origin` on an upgrade**, so a page on another
    site that opens a socket here says so, and is refused even with the
    token: the token stops being the only guard the moment a cookie can
    stand in for it. A request with no `Origin` is no browser's, and the
    token alone decides. The host compared is `Host`, which a reverse
    proxy has to pass through unchanged.
    """
    if origin is None:
        return True
    return host is not None and urlsplit(origin).netloc.lower() == host.lower()


def a_token() -> str:
    "A new token."
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_file() -> Path:
    "Where the token is kept between runs: the state directory, on any platform."
    return user_state_path("pymux") / "web-token"


def kept_token(path: Path) -> str:
    """
    The token in `path`, or a new one written there.

    **Kept, so a bookmark outlives a restart.** A token per run broke
    every saved address each time the service started again. Delete the
    file to make a new one.

    **Written 0600 and exclusively.** The file is the key to the
    terminal, and `O_EXCL` refuses a file somebody else put there first.
    """
    try:
        kept = path.read_text().strip()
    except FileNotFoundError:
        kept = ""
    if kept:
        return kept
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = a_token()
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as written:
        written.write(token + "\n")
    return token


@functools.cache
def _icon(size: int) -> bytes:
    return resvg_py.svg_to_bytes(svg_path=str(STATIC / "icon.svg"), width=size, height=size)


def _served(path: str):
    "The file and its type for one request path, or None."
    size = ICON_SIZES.get(path)
    if size is not None:
        return _icon(size), "image/png"
    font = FONT.fullmatch(path)
    if font is not None:
        try:
            return (STATIC / "fonts" / font.group(1)).read_bytes(), "font/woff2"
        except OSError:
            return None
    held = SERVED.get(path)
    if held is None:
        return None
    name, kind = held
    try:
        return (STATIC / name).read_bytes(), kind
    except OSError:
        # A package built without fonts serves an empty sheet, not a 404.
        return (b"", kind) if name == "fonts.css" else None


async def serve(
    socket_path: str,
    host: str,
    port: int,
    token: str,
    writable: bool = False,
    ready: Callable[[], None] | None = None,
) -> None:
    """
    Serve panes of that pymux server until this is cancelled.

    `writable` is what a viewer may do, and it is off by default: a port
    that types into a terminal is a different thing from one that shows
    it, and the safe one is the one nobody has to remember to ask for.

    `ready` is called once the port is listening, and it is why it exists:
    the command printed its URL and token first and then failed to bind,
    so a printed URL said nothing about whether anything was there. A
    caller found that by having something else on the port.
    """
    try:
        from websockets.asyncio.server import serve as websocket_serve
        from websockets.datastructures import Headers
        from websockets.http11 import Response
    except ImportError as missing:
        raise RuntimeError(MISSING) from missing

    def check(connection: ServerConnection, request: Request) -> Response | None:
        """
        Answer anything that is not a websocket upgrade, and refuse an
        upgrade that carries no token.

        **The token is checked here**, before a connection exists, so a
        caller with no token never reaches a pane.
        """
        path, _, query = request.path.partition("?")

        held = _served(path)
        if held is not None:
            body, kind = held
            return Response(
                200,
                "OK",
                Headers(
                    {
                        "Content-Type": kind,
                        "Content-Length": str(len(body)),
                        **GUARDS,
                        **(KEPT if FONT.fullmatch(path) else {}),
                    }
                ),
                body,
            )

        if not (path.startswith("/pane/") or path == SESSION):
            return guarded(connection.respond(404, "no such thing here\n"))

        asked = _one_of(query, "t")
        if not asked or not secrets.compare_digest(asked, token):
            # The same answer for a missing token and a wrong one.
            logger.warning("A web connection from %s arrived without the token.", connection.remote_address)
            return guarded(connection.respond(403, "the token is wrong or missing\n"))

        if not same_origin(request.headers.get("Origin"), request.headers.get("Host")):
            logger.warning(
                "A web connection from %s came from another site's page: %s.",
                connection.remote_address,
                request.headers.get("Origin"),
            )
            return guarded(connection.respond(403, "this page is not the server's own\n"))

        return None

    def guarded(response: Response) -> Response:
        for name, value in GUARDS.items():
            response.headers[name] = value
        return response

    async def handle(connection: ServerConnection) -> None:
        # A handler runs after the handshake, which is what sets the request.
        assert connection.request is not None
        path, _, query = connection.request.path.partition("?")
        if path == SESSION:
            await _serve_a_session(socket_path, connection, query, writable)
            return

        pane_id = path[len("/pane/") :]
        if not pane_id:
            await connection.close(1008, "no pane named")
            return

        logger.info("A viewer opened %s.", pane_id)
        try:
            async with PaneStream(socket_path, pane_id, writable=writable) as stream:
                await _relay(stream, connection)
        except StreamRefused as refused:
            # **The reason travels, because a silent close says nothing.**
            # A pane id the server cannot find -- a percent-encoding a
            # caller did not undo, a pane that has gone -- closed with
            # code 1000 and no welcome, which reads as a stream that
            # simply ended. 1008 is "policy violation", which is what
            # websockets have instead of a 404 once the upgrade is done.
            logger.info("A viewer asked for %s: %s", pane_id, refused)
            await connection.close(1008, str(refused)[:120])
        except Exception as ended:
            logger.info("A viewer of %s ended: %s", pane_id, ended)

    async with websocket_serve(handle, host, port, process_request=check):
        import anyio

        if ready is not None:
            ready()
        await anyio.sleep_forever()


async def _relay(stream: PaneStream, connection: ServerConnection) -> None:
    """
    Copy frames one way and messages the other, until either end stops.

    **Two tasks, because both directions block.** A viewer that says
    nothing must still be sent frames, and a pane that says nothing must
    still take input.
    """
    import anyio

    async with anyio.create_task_group() as both:

        async def to_the_viewer() -> None:
            async for frame in stream:
                await connection.send(json.dumps(frame))
            both.cancel_scope.cancel()

        async def to_the_pane() -> None:
            async for said in connection:
                # Passed on as the viewer said it: the frames are the
                # protocol and this is a relay. The server answers about
                # anything it will not do. Text that is no JSON at all
                # closes with the reason, so a viewer can tell it from a
                # stream that ended.
                try:
                    message = json.loads(said)
                except ValueError:
                    await connection.close(1003, "that is not JSON")
                    break
                await stream.send(message)
            both.cancel_scope.cancel()

        both.start_soon(to_the_viewer)
        both.start_soon(to_the_pane)


async def _serve_a_session(socket_path: str, connection: ServerConnection, query: str, writable: bool) -> None:
    """
    One viewer as one client of the session, with everything a client
    draws. `rows` and `columns` in the query are the element's first size;
    a `size` message moves it after. Lillecarl/pyterm#483. `options` is
    the client options the page kept, as JSON. Lillecarl/pyterm#959.
    """
    import anyio

    from pymux.web.session import LARGEST, client_options_of, run_session

    def cells(name: str, default: int) -> int:
        try:
            value = int(_one_of(query, name) or default)
        except ValueError:
            return default
        return max(1, min(LARGEST, value))

    logger.info("A viewer opened the session.")
    try:
        stream = await anyio.connect_unix(socket_path)
    except OSError as refused:
        await connection.close(1008, str(refused)[:120])
        return

    async def to_viewer(frame: dict[str, Any]) -> None:
        await connection.send(json.dumps(frame))

    try:
        await run_session(
            stream,
            to_viewer,
            connection,
            cells("rows", 24),
            cells("columns", 80),
            read_only=not writable,
            client_options=client_options_of(_one_of(query, "options") or ""),
        )
    except Exception as ended:
        logger.info("A viewer of the session ended: %s", ended)
    finally:
        await stream.aclose()


def _one_of(query: str, name: str) -> str | None:
    "One value out of a query string, without importing a URL parser."
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return value
    return None


async def the_server(socket_path: str | None) -> Server:
    "The pymux server to serve panes of."
    if socket_path:
        return Server(socket_path)
    return await Server.first()
