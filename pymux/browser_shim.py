"""
`pymux-browser`: a Chromium for a program in a pane, which runs on the
machine of the person instead. Lillecarl/pymux#653.

Name it as the browser to launch -- `PUPPETEER_EXECUTABLE_PATH`, or
chromedriver's binary -- or run it bare and give a tool the endpoint it
prints, for `connectOverCDP` or `puppeteer.connect`. It asks the server
for a browser (`launch-browser`), then answers the way Chrome itself
does once it listens:

- "DevTools listening on ws://..." on stderr, which Puppeteer reads,
  or "WebDriver BiDi listening on ws://..." when the browser is a
  Firefox;
- for a Chromium, `DevToolsActivePort` in the `--user-data-dir` it was
  given, which chromedriver reads.

The address is this machine's: the port is forwarded from here to the
browser, and it is the same number at both ends. Then it holds the
browser with `hold-browser` until one of them ends. Killing it closes
the browser, because the server stops a browser whose hold went.

**Playwright's `launch()` talks over a pipe** (`--remote-debugging-pipe`):
NUL-terminated CDP messages on fd 3 in, fd 4 out. Then this carries each
message to the browser's WebSocket and back, for as long as both stay.
A Firefox has no CDP, so that needs a Chromium. Lillecarl/pymux#655.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import sys
import threading
from pathlib import Path

import anyio

__all__ = ["bridge", "main"]

#: Playwright's pipe: what it writes to, what it reads from, and what
#: ends a message on both.
PIPE_IN = 3
PIPE_OUT = 4
END = b"\0"


def _value_of(argument: str, name: str) -> str | None:
    "The value of `--name=value`, or None when `argument` is another."
    prefix = name + "="
    return argument[len(prefix) :] if argument.startswith(prefix) else None


def _run(socket_name: str, command: str, pane_id: str | None) -> tuple[int, str]:
    "Run a command on the server: its exit code and what it printed."
    from pymux.client import create_client

    client = create_client(socket_name)
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        code = client.run_command(command, pane_id)
    return code, printed.getvalue()


async def _write_all(fd: int, data: bytes) -> None:
    while data:
        await anyio.wait_writable(fd)
        try:
            data = data[os.write(fd, data) :]
        except BlockingIOError:
            continue


async def bridge(url: str, read_fd: int = PIPE_IN, write_fd: int = PIPE_OUT) -> None:
    "Carry CDP messages between a pipe and the browser's WebSocket until either side ends."
    from websockets.asyncio.client import connect

    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    # No limit: a screenshot is one message of megabytes.
    async with connect(url, max_size=None) as socket, anyio.create_task_group() as tasks:

        async def to_browser() -> None:
            pending = b""
            while True:
                await anyio.wait_readable(read_fd)
                try:
                    data = os.read(read_fd, 1 << 16)
                except BlockingIOError:
                    continue
                if not data:
                    break
                *messages, pending = (pending + data).split(END)
                for message in messages:
                    await socket.send(message.decode("utf-8"))
            tasks.cancel_scope.cancel()

        async def to_program() -> None:
            async for message in socket:
                await _write_all(write_fd, (message.encode("utf-8") if isinstance(message, str) else message) + END)
            tasks.cancel_scope.cancel()

        tasks.start_soon(to_browser)
        tasks.start_soon(to_program)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    piped = "--remote-debugging-pipe" in arguments
    arguments = [one for one in arguments if one != "--remote-debugging-pipe"]
    profile = None
    for argument in arguments:
        found = _value_of(argument, "--user-data-dir")
        if found is not None:
            profile = Path(found)

    address = os.environ.get("PYMUX", "")
    if not address:
        sys.stderr.write("pymux-browser: not in a pymux pane: $PYMUX is not set\n")
        return 1
    socket_name, _, pane_id = address.partition(",")

    command = "launch-browser -- %s" % " ".join(shlex.quote(one) for one in arguments)
    code, printed = _run(socket_name, command, pane_id or None)
    if code != 0:
        return code
    port, path, launch, remote = printed.split()

    endpoint = "ws://127.0.0.1:%s%s" % (port, path)
    if piped:
        if remote != "cdp":
            sys.stderr.write("pymux-browser: --remote-debugging-pipe needs a Chromium; set browser-program to one\n")
            _run(socket_name, "hold-browser -s %s" % (launch,), pane_id or None)
            return 1
        # The hold ends the browser when this process goes, so it only
        # has to stay open beside the bridge.
        threading.Thread(
            target=_run, args=(socket_name, "hold-browser %s" % (launch,), pane_id or None), daemon=True
        ).start()
        anyio.run(bridge, endpoint, PIPE_IN, PIPE_OUT)
        return 0
    if remote == "bidi":
        # Firefox names its port alone, and Puppeteer adds the path.
        sys.stderr.write("\nWebDriver BiDi listening on ws://127.0.0.1:%s\n" % (port,))
    else:
        if profile is not None:
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "DevToolsActivePort").write_text("%s\n%s\n" % (port, path))
        sys.stderr.write("\nDevTools listening on %s\n" % (endpoint,))
    sys.stderr.flush()
    if sys.stdout.isatty():
        sys.stdout.write("%s\n" % (endpoint,))
        sys.stdout.flush()

    code, _ = _run(socket_name, "hold-browser %s" % (launch,), pane_id or None)
    return code


if __name__ == "__main__":
    sys.exit(main())
