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

`--remote-debugging-pipe`, which Playwright's `launch()` uses, is not
served yet: Lillecarl/pymux#655.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import sys
from pathlib import Path

__all__ = ["main"]


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


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--remote-debugging-pipe" in arguments:
        sys.stderr.write(
            "pymux-browser: --remote-debugging-pipe is not served yet (Lillecarl/pymux#655);"
            " use --remote-debugging-port or connectOverCDP\n"
        )
        return 2
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
