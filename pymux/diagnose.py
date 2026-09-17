"""
One report of what this machine sees, in --human and --json.

Every "works in tmux, not in pymux" conversation starts with the same
facts: the TERM and whether a terminfo entry answers it, the locale,
the socket and whether a server is listening, and whether the outer
terminal answers a query at all. Today they are gathered by hand.
This module gathers them once, so a person pastes one output.
Lillecarl/pymux#391.

**Everything here is read-only.** The farthest this goes into a server
is a `list-sessions`, which is what a detached command does anyway.

**The imports are all stdlib and all light.** `run_pymux` reaches this
module on a mode word, and a detached command must not pay for a
toolkit it never draws with -- the same rule
`tests/test_entry_imports.py` holds for the entry itself.
Lillecarl/pymux#392.
"""

import curses
import json
import locale
import os
import select
import socket
import sys
import termios
import time

__all__ = ["diagnose", "human", "as_json"]

#: How long the outer terminal has to answer a query.
REPLY_TIMEOUT = 0.4


def diagnose(socket_name: str | None, config_file: str | None) -> dict:
    """
    The facts, as one dict of plain values.
    """
    report: dict = {
        "pymux": {
            "version": _pymux_version(),
            "python": sys.version.split()[0],
            "interface": "tmux 3.4",
            "inside_pymux": bool(os.environ.get("PYMUX")),
        },
        "environment": {
            "TERM": os.environ.get("TERM") or "",
            "terminfo_entry": _has_terminfo_entry(os.environ.get("TERM") or ""),
            "locale": _locale_report(),
        },
        "configuration": {
            "file": config_file,
            "exists": bool(config_file) and os.path.exists(config_file),
        },
        "socket": _socket_report(socket_name),
        "outer_terminal": _terminal_report(),
    }
    return report


def _pymux_version() -> str:
    from pymux import __version__

    return __version__


def _has_terminfo_entry(term: str) -> bool:
    "Whether a terminfo entry answers this TERM at all."
    if not term:
        return False
    try:
        curses.setupterm(term)
        return True
    except Exception:
        return False


def _locale_report() -> dict:
    "What the locale machinery answers, and what a pane would encode with."
    names = {
        name: os.environ.get(name) or ""
        for name in ("LC_ALL", "LC_CTYPE", "LANG")
        if os.environ.get(name)
    }
    try:
        preferred = locale.getpreferredencoding(False)
    except Exception:
        preferred = ""
    return {"env": names, "preferred_encoding": preferred}


def _socket_report(socket_name: str | None) -> dict:
    """
    Where the socket is, and whether a server is listening on it.

    **A connect is the only question asked.** A server that answers a
    connect answers `list-sessions` too, and its output is what says
    how many sessions it holds.
    """
    from pymux.pipes.posix import socket_directory

    report: dict = {
        "name": socket_name or "",
        "directory": socket_directory(),
        "exists": False,
        "connectable": False,
        "sessions": None,
        "error": "",
    }
    if not socket_name:
        report["error"] = "no socket named: no -S and no $PYMUX"
        return report
    report["exists"] = os.path.exists(socket_name)
    if not report["exists"]:
        report["error"] = "no server running on %s" % socket_name
        return report

    # The import rides here and not at the top of the module: the
    # client package is the detachable part of the entry, and a
    # diagnose that never reaches a server pays nothing for it.
    # Lillecarl/pymux#392.
    from pymux.client import create_client

    try:
        client = create_client(socket_name)
    except OSError as error:
        report["error"] = str(error)
        return report

    report["connectable"] = True
    # A socket that answers the connect and nothing else -- some other
    # daemon on a mistyped path -- must not hold the report hostage:
    # two seconds, and then it is called mute.
    answer = _io_capture(
        lambda: client.run_command("list-sessions", timeout=2.0)
    )
    report["sessions"] = answer.strip() or None
    report["error"] = "" if report["sessions"] else "connected, but no answer came"
    return report


def _io_capture(run) -> str:
    "What `run` printed to stdout, as a string."
    import io

    was = sys.stdout
    sys.stdout = io.StringIO()
    try:
        run()
        return sys.stdout.getvalue()
    finally:
        sys.stdout = was


def _terminal_report() -> dict:
    """
    Whether the outer terminal answers a query, and which.

    Asked only when the output is a terminal of its own: a piped run
    has no terminal to ask, and a run inside pymux would be asking the
    pane instead of the machine's terminal. The questions are the two
    every terminal answers: a DA1 and an OSC 11. The tty is put in raw
    mode for the read and put back before this returns.
    """
    report = {"asked": False, "replied": False, "what": ""}
    if not sys.stdout.isatty() or os.environ.get("PYMUX"):
        return report

    report["asked"] = True
    stdin = sys.stdin.fileno()
    try:
        was = termios.tcgetattr(stdin)
    except Exception:
        return report

    try:
        # Raw mode, so the answer is read whole and not echoed or
        # line-buffered into something else.
        raw = termios.tcgetattr(stdin)
        raw[3] &= ~(
            termios.ECHO
            | termios.ICANON
            | termios.ISIG
            | termios.IEXTEN
        )
        termios.tcsetattr(stdin, termios.TCSANOW, raw)

        sys.stdout.write("\x1b[c")  # DA1: what are you?
        sys.stdout.write("\x1b]11;?\x1b\\")  # OSC 11: what is the background?
        sys.stdout.flush()

        deadline = time.monotonic() + REPLY_TIMEOUT
        seen = b""
        while time.monotonic() < deadline:
            left, _, _ = select.select([stdin], [], [], 0.05)
            if left:
                seen += os.read(stdin, 4096)

        report["replied"] = bool(seen)
        what = []
        if b"\x1b[?" in seen:
            what.append("da1")
        if b"\x1b]11;" in seen:
            what.append("osc11")
        report["what"] = " ".join(what)
    except Exception as error:
        report["what"] = "error: %s" % error
    finally:
        termios.tcsetattr(stdin, termios.TCSANOW, was)
    return report


def human(report: dict) -> str:
    """
    The report as the lines a person reads.
    """
    lines = []

    def say(label: str, value) -> None:
        if isinstance(value, dict):
            lines.append("%s:" % label)
            for name, inner in value.items():
                lines.append("  %s: %s" % (name, inner))
        else:
            lines.append("%s: %s" % (label, value))

    say("pymux", report["pymux"])
    say("environment", report["environment"])
    say("configuration", report["configuration"])
    say("socket", report["socket"])
    say("outer terminal", report["outer_terminal"])
    return "\n".join(lines)


def as_json(report: dict) -> str:
    "The report as JSON, for a script or an agent."
    return json.dumps(report, indent=2)
