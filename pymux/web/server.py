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
token that this prints once. `--bind` widens it and says what that costs.

It needs `pymux[web]`. Lillecarl/pymux#461.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from pathlib import Path

from libpymux import PaneStream, Server, StreamRefused

from pymux.log import logger

__all__ = ["MISSING", "a_token", "serve"]

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
#: the panes. Lillecarl/pymux#481.
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
}


def a_token() -> str:
    "A token for one run of the web server."
    return secrets.token_urlsafe(TOKEN_BYTES)


def _served(path: str):
    "The file and its type for one request path, or None."
    held = SERVED.get(path)
    if held is None:
        return None
    name, kind = held
    try:
        return (STATIC / name).read_bytes(), kind
    except OSError:
        return None


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

    def check(connection, request):
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
                        # The page loads the element and nothing else,
                        # and the element needs no `unsafe-inline` of any
                        # kind: its rules go through the CSSOM.
                        "Content-Security-Policy": "default-src 'self'",
                    }
                ),
                body,
            )

        if not (path.startswith("/pane/") or path == SESSION):
            return connection.respond(404, "no such thing here\n")

        asked = _one_of(query, "t")
        if not asked or not secrets.compare_digest(asked, token):
            # The same answer for a missing token and a wrong one.
            logger.warning("A web connection arrived without the token.")
            return connection.respond(403, "the token is wrong or missing\n")

        return None

    async def handle(connection) -> None:
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


async def _relay(stream: PaneStream, connection) -> None:
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
                # Passed through without being read: the frames are the
                # protocol and this is a relay. The server answers about
                # anything it will not do.
                await stream.send(json.loads(said))
            both.cancel_scope.cancel()

        both.start_soon(to_the_viewer)
        both.start_soon(to_the_pane)


async def _serve_a_session(socket_path: str, connection, query: str, writable: bool) -> None:
    """
    One viewer as one client of the session, with everything a client
    draws. `rows` and `columns` in the query are the element's first size;
    a `size` message moves it after. Lillecarl/pymux#481.
    """
    import anyio

    from pymux.web.session import LARGEST, run_session

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

    async def to_viewer(frame) -> None:
        await connection.send(json.dumps(frame))

    try:
        await run_session(
            stream,
            to_viewer,
            connection,
            cells("rows", 24),
            cells("columns", 80),
            read_only=not writable,
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


def the_server(socket_path: str | None) -> Server:
    "The pymux server to serve panes of."
    if socket_path:
        return Server(socket_path)
    return Server.first()
