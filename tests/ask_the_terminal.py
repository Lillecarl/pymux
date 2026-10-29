"""
Ask a real terminal what state pymux left it in.

A cell check reads pymux's bytes, and a picture reads the terminal's
pixels. Neither asks the terminal for its modes, and three faults came
from there: iTerm2 left the alternate screen on a forwarded OSC 133;A,
answered 4 for DECRQM 1016, and kept the click that focuses its window.
Lillecarl/pymux#800.

So this attaches a client in each terminal, with a pane that writes the
OSCs pymux forwards, and asks the terminal for its modes twice through
the relay (`drive_in_terminal.py`, "Asking the terminal"):

* attached: each mode as pymux last set it, by the bytes it wrote;
* detached: every mode off again, but the cursor on.

A terminal that answers 0 does not know the mode, and says nothing
about it.

    PYMUX_STATE_TERMINALS=kitty nix build --file . checks.pymux-terminal-state

The answers are kept in `$out/<terminal>/{attached,detached}.json`.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drive_in_terminal import ASKED_MODES
from middleman import run_cli
from photograph_chrome import CHROME, CHROME_TERMINALS, RELAY
from pyterm_pytest.seats import TheSeatIsGone, open_the_seats, with_no_answer
from take_picture import every_log

OUT = Path(os.environ.get("PYMUX_STATE_OUT", "terminal-state"))
ONLY_TERMINALS = os.environ.get("PYMUX_STATE_TERMINALS", "")

#: How long the relay copies, in seconds: a bound on the whole run.
HOLD = 120

#: DECRPM: 1 set, 2 reset, 3 always set, 4 always reset, 0 unknown.
SET = (1, 3)
UNKNOWN = 0

#: The one mode a detached terminal has on: the cursor shows.
ON_AFTER = frozenset({25})

#: The OSCs pymux forwards to the outer terminal, then a line to wait for.
PANE = r"""printf '\033]133;A\033\\'
printf '\033]0;a title\007'
printf '\033]7;file://localhost/tmp\033\\'
printf '\033]133;B\033\\'
printf '\033]99;i=1;hello\033\\'
printf 'ready\n'
exec sleep 900
"""


def until(what, question, timeout=30.0):
    "Ask `question` until it answers something true, and give that back."
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = question()
        if last:
            return last
        time.sleep(0.2)
    raise RuntimeError("%s never happened; last answer: %r" % (what, last))


def answer(socket_path, *argv):
    done = run_cli(socket_path, list(argv))
    if done.returncode != 0:
        raise RuntimeError("%s: %s" % (shlex.join(argv), done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout.decode("utf-8", "replace")


def report_of(path):
    "The relay's report, once it is there."
    if not path.exists():
        return None
    report = json.loads(path.read_text())
    return {kind: {int(mode): value for mode, value in found.items()} for kind, found in report.items()}


def wrong_while_attached(report):
    "Each mode the terminal holds otherwise than pymux last set it."
    wrong = []
    for mode in ASKED_MODES:
        meant = report["meant"].get(mode)
        said = report["answers"].get(mode, UNKNOWN)
        if meant is None or said == UNKNOWN:
            continue
        if (said in SET) != meant:
            wrong.append("%d is %s, where pymux %s it" % (mode, said, "set" if meant else "reset"))
    return wrong


def wrong_after_detach(report):
    "Each mode a detached terminal still holds otherwise than it started."
    wrong = []
    for mode in ASKED_MODES:
        said = report["answers"].get(mode, UNKNOWN)
        if said == UNKNOWN:
            continue
        if (said in SET) != (mode in ON_AFTER):
            wrong.append("%d is %s after the detach" % (mode, said))
    return wrong


def ask_in(terminal, seat, work, out):
    "The whole run in one terminal: what it holds wrong, as lines."
    room = out / terminal.name
    room.mkdir(parents=True, exist_ok=True)
    socket_path = work / ("%s.sock" % terminal.name)
    pane = work / "pane.sh"
    pane.write_text(PANE)
    config = work / "state.conf"
    config.write_text(CHROME)
    keys_path = work / "none.keys"
    keys_path.write_text("")
    # Not in `room`: nix refuses an output that holds the relay's fifo.
    asks = work / ("%s-asks" % terminal.name)

    started = run_cli(
        socket_path,
        [
            *("-f", str(config), "--log", str(room / "server.log")),
            *("new-session", "-d", "-s", "test", "sh %s" % pane),
        ],
    )
    if started.returncode != 0:
        raise RuntimeError("the server did not start: %s" % started.stderr.decode())

    attach = "exec python3 -m pymux -S %s -f %s attach 2>%s" % (
        shlex.quote(str(socket_path)),
        shlex.quote(str(config)),
        shlex.quote(str(room / "client-stderr.log")),
    )
    command = "PYMUX_RELAY_ASK=%s exec python3 %s %s %d 2>%s -- sh -c %s" % (
        shlex.quote(str(asks)),
        shlex.quote(str(RELAY)),
        shlex.quote(str(keys_path)),
        HOLD,
        shlex.quote(str(room / "relay-error.log")),
        shlex.quote(attach),
    )

    def director(take_one, ended):
        until("one client", lambda: answer(socket_path, "list-clients", "-F", "#{client_name}").split())
        until("the pane's line", lambda: "ready" in answer(socket_path, "capture-pane", "-p"))
        answer(socket_path, "refresh-client")
        answer(socket_path, "display-message", "asking")
        take_one(room / "attached.png")
        until("the ask's fifo", (asks / "ask").exists)
        with open(asks / "ask", "w") as fifo:
            fifo.write("attached\n")
        attached = until("the attached answers", lambda: report_of(asks / "attached.json"))
        answer(socket_path, "detach-client", "-s", "test")
        detached = until("the detached answers", lambda: report_of(asks / "detached.json"))
        take_one(room / "detached.png")
        for name, report in (("attached", attached), ("detached", detached)):
            (room / ("%s.json" % name)).write_text((asks / ("%s.json" % name)).read_text())
            if not report["answers"]:
                raise RuntimeError("the terminal answered no mode %s" % name)
        return wrong_while_attached(attached) + wrong_after_detach(detached)

    try:
        return seat.running(terminal, command, work, room / "seat.log", director)
    finally:
        run_cli(socket_path, ["kill-server"])


def main():
    terminals = [one for one in CHROME_TERMINALS if ONLY_TERMINALS in one.name]
    if not terminals:
        raise SystemExit("no terminal holds %r" % ONLY_TERMINALS)
    missing = [one.name for one in terminals if not one.is_available()]
    if missing:
        raise SystemExit("these terminals are not here: %s" % ", ".join(missing))

    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-state"
    work.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    try:
        seats = open_the_seats(terminals, work)
    except TheSeatIsGone as reason:
        return with_no_answer(str(reason))

    failed = []
    try:
        for terminal in terminals:
            try:
                wrong = ask_in(terminal, seats[terminal.seat], work, OUT)
            except RuntimeError as reason:
                try:
                    seats[terminal.seat].still_there()
                except TheSeatIsGone as gone:
                    return with_no_answer(str(gone))
                failed.append(terminal.name)
                print("%s: %s\n%s" % (terminal.name, reason, every_log(OUT / terminal.name)), flush=True)
                continue
            if wrong:
                failed.append(terminal.name)
            print("%s: %s" % (terminal.name, "; ".join(wrong) or "every mode as pymux meant it"), flush=True)
    except TheSeatIsGone as gone:
        return with_no_answer(str(gone))
    finally:
        for seat in seats.values():
            seat.keep_the_log(OUT)
            seat.stop()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
