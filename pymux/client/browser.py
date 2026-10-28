"""
The browser a program on the server drives, started on the machine the
person sits at. Lillecarl/pymux#653.

A Playwright or Puppeteer script, or an agent's browser tool, in a pane
drives a browser over a remote protocol. That browser belongs on the
screen of the person, so the server asks the client to start one with
`--remote-debugging-port`, and the program on the server reaches it
through a forward. The port is the same number on both machines: Chrome
names its own address in `/json/version`, and a tool that reads it there
has to be able to use it.

**Two kinds.** A Chromium speaks the Chrome DevTools Protocol, which
Playwright, Puppeteer and most agent tools drive. Firefox speaks
WebDriver BiDi, which Puppeteer, Selenium 4 and WebdriverIO drive, and
Playwright does not.

**The endpoint comes from the browser's own line**, "DevTools listening
on ws://..." or "WebDriver BiDi listening on ws://...", which it writes
to stderr once it listens, and which goes to `LOG` in the profile. Not a
pipe: a browser writes a lot there, and a pipe nobody drains stops it.
Chromium's `DevToolsActivePort` says the same, but Chromium 153 writes
it only for `--remote-debugging-port=0`, measured.

**On macOS an app starts through `open`**, as its own app. Started as a
child of a client that Nix built, macOS counts it as that client and
keeps it out of `~/Library/Application Support`, and Firefox ends with
"Could not find profile folder", measured. `open -W` lasts as long as
the app; the app itself is found by its profile, which is this launch's
alone, when it has to stop.

**The profile is never the person's own.** Chrome 136 and later refuse
remote debugging on the default profile, and a program that drives a
browser should not hold the person's logins anyway. `browser-profile`
names another; the default is one pymux keeps, one per kind.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from anyio.abc import Process

__all__ = [
    "START_SECONDS",
    "BrowserError",
    "Remote",
    "argv_of",
    "end",
    "endpoint_in",
    "kind_of",
    "profile_of",
    "program_of",
    "start",
    "start_async",
]

#: How long a browser has to say that it listens.
START_SECONDS = 20.0

#: How often the profile is read while a browser starts.
POLL_SECONDS = 0.05

#: Where in the profile the browser's stderr goes.
LOG = "pymux-browser.log"


class Remote(StrEnum):
    "The protocol a browser is driven over."

    CDP = "cdp"
    BIDI = "bidi"

    @property
    def listening(self) -> str:
        "What the browser writes once it listens on a port, before the path of its endpoint."
        if self is Remote.CDP:
            return "DevTools listening on ws://127.0.0.1:%d"
        return "WebDriver BiDi listening on ws://127.0.0.1:%d"


#: The browsers looked for when the person named none, in order: every
#: Chromium before Firefox, because more tools speak CDP.
CANDIDATES = {
    "darwin": (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Firefox.app/Contents/MacOS/firefox",
    ),
    "linux": (
        "chromium",
        "chromium-browser",
        "google-chrome-stable",
        "google-chrome",
        "microsoft-edge",
        "brave",
        "firefox",
    ),
}

#: The arguments this side sets itself, which the program's are not
#: allowed to move: where the browser listens and which profile it uses.
OURS = (
    "--remote-debugging-port",
    "--remote-debugging-pipe",
    "--remote-debugging-address",
    "--user-data-dir",
    "--profile",
    "-profile",
)


class BrowserError(Exception):
    "The browser did not start, and why, for the program on the server to print."


def program_of(asked: str) -> str:
    "The browser to run: the one the person named, or the first one found here."
    if asked:
        found = shutil.which(os.path.expanduser(asked))
        if found is None:
            raise BrowserError("browser-program %r is not here" % (asked,))
        return found
    for candidate in CANDIDATES.get(sys.platform, CANDIDATES["linux"]):
        found = shutil.which(candidate)
        if found is not None:
            return found
    raise BrowserError("no Chromium or Firefox here; set browser-program to one")


def kind_of(program: str) -> Remote:
    "The protocol this browser speaks: BiDi for a Firefox, CDP for the rest."
    return Remote.BIDI if "firefox" in os.path.basename(program).lower() else Remote.CDP


def profile_of(asked: str, kind: Remote) -> Path:
    "The profile directory the browser runs with, made when it is not there."
    name = "browser" if kind is Remote.CDP else "browser-firefox"
    if asked:
        profile = Path(asked).expanduser()
    elif sys.platform == "darwin":
        profile = Path("~/Library/Application Support/pymux").expanduser() / name
    else:
        state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        profile = Path(state) / "pymux" / name
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def argv_of(program: str, kind: Remote, port: int, profile: Path, arguments: list[str]) -> list[str]:
    "The command line of the browser: ours, then the program's, without any that would move ours."
    theirs = [one for one in arguments if one.split("=", 1)[0] not in OURS]
    if kind is Remote.BIDI:
        # Chromium's spelling of headless, which a CDP tool passes.
        theirs = ["--headless" if one.startswith("--headless=") else one for one in theirs]
        return [program, "--remote-debugging-port", str(port), "--profile", str(profile), "--no-remote", *theirs]
    return [
        program,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        *theirs,
    ]


def endpoint_in(profile: Path, port: int, kind: Remote) -> str | None:
    "The path of the browser's endpoint, once a browser on `port` said it listens; else None."
    if kind is Remote.CDP:
        with contextlib.suppress(OSError):
            lines = (profile / "DevToolsActivePort").read_text().splitlines()
            if len(lines) >= 2 and lines[0].strip() == str(port):
                return lines[1].strip()
    with contextlib.suppress(OSError):
        said = (profile / LOG).read_text(errors="replace")
        line = kind.listening % port
        at = said.find(line)
        if at >= 0:
            rest = said[at + len(line) :].split("\n", 1)[0].strip()
            if kind is Remote.BIDI:
                # Firefox names the port alone; BiDi sessions start here.
                return "/session"
            if rest.startswith("/"):
                return rest
    return None


def bundle_of(program: str) -> str | None:
    "The macOS app a program is the binary of, or None."
    if sys.platform != "darwin" or ".app/Contents/MacOS/" not in program:
        return None
    return program.split(".app/Contents/MacOS/", 1)[0] + ".app"


def _prepared(program: str, profile: str, port: int, arguments: list[str]) -> tuple[list[str], Path, Remote]:
    found = program_of(program)
    kind = kind_of(found)
    where = profile_of(profile, kind)
    # What a browser before left: its port is not this one, but a
    # browser that crashed on this one would read as listening.
    (where / "DevToolsActivePort").unlink(missing_ok=True)
    (where / LOG).unlink(missing_ok=True)
    argv = argv_of(found, kind, port, where, arguments)
    bundle = bundle_of(found)
    if bundle is not None:
        argv = ["open", "-n", "-W", "-a", bundle, "--stderr", str(where / LOG), "--args", *argv[1:]]
    return argv, where, kind


def _end_app(profile: Path) -> None:
    listed = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    for row in listed.splitlines():
        pid, _, command = row.strip().partition(" ")
        if pid.isdigit() and str(profile) in command and not command.startswith("open "):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGTERM)


def end(process: subprocess.Popen[bytes] | Process, profile: Path) -> None:
    """
    Stop a browser. On macOS that is the app `open` started, found by its
    profile on a thread of its own, so no loop waits on `ps`; `open`
    ends with it.
    """
    if sys.platform == "darwin":
        threading.Thread(target=_end_app, args=(profile,), daemon=True).start()
    else:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()


def _did_not_listen(argv: list[str], code: int | None) -> BrowserError:
    if code is not None:
        # Chrome hands a second start on a profile that runs already to
        # the running one, and exits 0 without listening.
        return BrowserError(
            "%s ended with %d before it listened; is it running already with this profile?"
            % (os.path.basename(argv[0]), code)
        )
    return BrowserError("%s did not listen within %d seconds" % (os.path.basename(argv[0]), START_SECONDS))


def start(
    program: str, profile: str, port: int, arguments: list[str]
) -> tuple[subprocess.Popen[bytes], str, Remote, Path]:
    """
    Start the browser and wait until it listens: the process, the
    endpoint's path, the protocol and the profile. For the client that has no event
    loop (`client/posix.py`); it stops drawing for the second this takes.
    """
    argv, where, kind = _prepared(program, profile, port, arguments)
    with open(where / LOG, "wb") as log:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True
        )
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        path = endpoint_in(where, port, kind)
        if path is not None:
            return process, path, kind, where
        if process.poll() is not None:
            raise _did_not_listen(argv, process.returncode)
        time.sleep(POLL_SECONDS)
    end(process, where)
    raise _did_not_listen(argv, None)


async def start_async(
    program: str, profile: str, port: int, arguments: list[str]
) -> tuple[anyio.abc.Process, str, Remote, Path]:
    "`start`, for a client with an event loop."
    argv, where, kind = _prepared(program, profile, port, arguments)
    with open(where / LOG, "wb") as log:
        process = await anyio.open_process(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True
        )
    with anyio.move_on_after(START_SECONDS):
        while True:
            path = endpoint_in(where, port, kind)
            if path is not None:
                return process, path, kind, where
            if process.returncode is not None:
                raise _did_not_listen(argv, process.returncode)
            await anyio.sleep(POLL_SECONDS)
    end(process, where)
    raise _did_not_listen(argv, None)
