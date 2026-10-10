"""
Photograph every view in a real terminal before and after `upgrade-server`.

A hot upgrade has to hand a person back exactly what they were looking
at, and only a terminal can say what that is. A cell check reads one
emulator's idea of the bytes; a terminal is stateful, and a redraw that
leaves a stale cell, a lost colour or a cursor in the wrong place shows
only in its pixels. So this runs one terminal for the whole check and
takes a picture of each view on both sides of the upgrade, and fails
unless every pair is the same pixels. Lillecarl/pymux#408.

What it builds, under one attached client:

* session `test`: a window split three ways, a window in strip mode
  with three columns, and a window with one of two panes zoomed;
* session `other`: two windows, and a popup over them;
* two finished jobs, one that failed;
* a pane in copy mode;
* a message on the client, and the view it is on at the upgrade, which
  the client has to come back to without being moved
  (Lillecarl/pymux#409).

The setup is typed at the client's prompt, because most of those
commands act on the window of the client that runs them. The views are
then reached with `switch-client -c`, so nothing is typed between the
pictures. The relay of `drive_in_terminal.py` holds the client, and the
client waits for the new server and attaches again on its own.

`test-mode` pins the clock in the status line, and the cursor is asked
not to blink: either would make two pictures of one view differ.

    PYMUX_UPGRADE_TERMINALS=foot nix build --file . checks.pymux-upgrade-pictures

The pictures are kept in `$out/<terminal>/<view>/{before,after}.png`,
with `difference.png` where they differ.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from middleman import run_cli
from photograph_chrome import CHROME, CHROME_TERMINALS, RELAY, difference_of, keys
from PIL import Image, ImageChops
from pyterm_pytest.seats import TheSeatIsGone, open_the_seats, with_no_answer
from take_picture import every_log

PICTURES = Path(os.environ.get("PYMUX_UPGRADE_OUT", "upgrade-pictures"))
ONLY_TERMINALS = os.environ.get("PYMUX_UPGRADE_TERMINALS", "foot")

#: How long the relay copies, in seconds: the whole check is one run.
HOLD = 600

#: The view the client is on at the upgrade, with a message on it, as the
#: client comes back to it.
LANDING = "landing"

#: Where the setup leaves the client: the last window it made in `test`.
START = "test:3"

#: How long a view may take to stop changing, in seconds.
SETTLE = 20.0

#: Prints its name in its colour, a few lines, a wide character and a
#: background, then waits.
SHOW = r"""printf '\033[1;3%sm%s\033[0m\n' "$2" "$1"
for i in 1 2 3; do printf '%s line %d\n' "$1" "$i"; done
printf '\033[4%sm wide 漢字 \033[0m\n' "$2"
exec sleep 900
"""

#: Every kind of view, made under the client as a person makes it.
SETUP = """\
split-window -h 'sh {show} second 2'
split-window -v 'sh {show} third 3'
rename-window built
new-window 'sh {show} strip-a 4'
set-window-option strip on
split-window -h 'sh {show} strip-b 5'
split-window -h 'sh {show} strip-c 6'
new-window 'sh {show} zoomed 1'
split-window -h 'sh {show} beside 2'
resize-pane -Z
new-session -d -s other 'sh {show} other 3'
switch-client -t other
new-window 'sh {show} other-two 4'
display-popup -w 40 -h 8 -T pop 'sh {show} popup 5'
switch-client -t test
"""

#: Panes per window once the setup has run, session by session in
#: window order. The popup belongs to its session and no window lists it.
PANES = {"test": [3, 3, 2], "other": [1, 1]}

JOBS = (
    ["run", "-w", "--tag", "kind=one", "printf one"],
    ["run", "-w", "--tag", "kind=two", "--tag", "loud", "printf two >&2; exit 4"],
)


def answer(socket_path, *argv):
    "What the server said, or a `RuntimeError` with why it said nothing."
    done = run_cli(socket_path, list(argv))
    if done.returncode != 0:
        raise RuntimeError("%s: %s" % (shlex.join(argv), done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout.decode("utf-8", "replace")


def until(what, question, timeout=30.0):
    "Ask `question` until it answers something true, and give that back."
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = question()
        except RuntimeError as reason:
            last = reason
        else:
            if last:
                return last
        time.sleep(0.2)
    raise RuntimeError("%s never happened; last answer: %r" % (what, last))


def panes_now(socket_path):
    "Panes per window, session by session in window order."
    counted: dict[str, dict[int, int]] = {}
    for line in answer(socket_path, "list-panes", "-a", "-F", "#{session_name} #{window_index}").splitlines():
        session, index = line.split()
        windows = counted.setdefault(session, {})
        windows[int(index)] = windows.get(int(index), 0) + 1
    return {session: [windows[index] for index in sorted(windows)] for session, windows in counted.items()}


def steady(take_one, path, timeout=SETTLE):
    """
    A picture of the screen once it has stopped changing.

    Three pictures in a row, a quarter second apart, the same pixels:
    the last frame of a view, and not one a redraw is halfway through.
    """
    deadline = time.monotonic() + timeout
    previous = None
    alike = 0
    while time.monotonic() < deadline:
        take_one(path)
        current = Image.open(path).convert("RGB")
        if previous is not None and ImageChops.difference(previous, current).getbbox() is None:
            alike += 1
            if alike == 2:
                return path
        else:
            alike = 0
        previous = current
        time.sleep(0.25)
    raise RuntimeError("the screen never held still for %s" % path.parent.name)


def every_view(socket_path, client, take_one, room, when):
    "A picture of each window of each session, as the client shows it."
    pictures = {}
    listed = answer(socket_path, "list-windows", "-a", "-F", "#{session_name}:#{window_index} #{window_id}")
    for line in listed.splitlines():
        view, window_id = line.split()
        answer(socket_path, "switch-client", "-c", client, "-t", window_id)
        directory = room / view.replace(":", "-")
        directory.mkdir(parents=True, exist_ok=True)
        pictures[view] = steady(take_one, directory / ("%s.png" % when))
    return pictures


def what_the_jobs_say(socket_path):
    return [
        answer(socket_path, "sql", "SELECT id, command, status, returncode, error, started, finished FROM jobs"),
        answer(socket_path, "sql", "SELECT job_id, key, value FROM job_tags ORDER BY job_id, key"),
        answer(socket_path, "show-job", "1"),
        run_cli(socket_path, ["show-job", "2"]).stdout,
    ]


def upgrade_in(terminal, seat, work, out):
    """
    The whole check in one terminal. Raises `RuntimeError` on a view
    that changed, and on anything that kept the pictures from being
    taken.
    """
    room = out / terminal.name
    room.mkdir(parents=True, exist_ok=True)
    socket_path = work / ("%s.sock" % terminal.name)
    show = work / "show.sh"
    show.write_text(SHOW)
    config = work / "upgrade.conf"
    config.write_text(CHROME)
    setup = work / "setup.conf"
    setup.write_text(SETUP.format(show=show))
    keys_path = work / "upgrade.keys"
    keys_path.write_text(keys((3.0, b"\x02"), (0.5, b":"), (0.6, ("source-file %s\r" % setup).encode())))

    started = run_cli(
        socket_path,
        [
            *("-f", str(config), "--log", str(room / "server.log")),
            *("new-session", "-d", "-s", "test", "sh %s first 1" % show),
        ],
    )
    if started.returncode != 0:
        raise RuntimeError("the server did not start: %s" % started.stderr.decode())
    # Before the client attaches: a finished job tells every client, and
    # this check chooses the message the client has.
    for args in JOBS:
        run_cli(socket_path, args)

    attach = "exec python3 -m pymux -S %s -f %s attach 2>%s" % (
        shlex.quote(str(socket_path)),
        shlex.quote(str(config)),
        shlex.quote(str(room / "client-stderr.log")),
    )
    command = "exec python3 %s %s %d 2>%s -- sh -c %s" % (
        shlex.quote(str(RELAY)),
        shlex.quote(str(keys_path)),
        HOLD,
        shlex.quote(str(room / "relay-error.log")),
        shlex.quote(attach),
    )

    def set_up():
        found = panes_now(socket_path)
        if found != PANES:
            raise RuntimeError("panes per window %r" % (found,))
        return True

    def director(take_one, ended):
        until("the setup", set_up)
        (client,) = until("one client", lambda: answer(socket_path, "list-clients", "-F", "#{client_name}").split())
        pid = answer(socket_path, "display-message", "-p", "#{pid}")
        jobs = what_the_jobs_say(socket_path)
        # Only a key clears a message or answers a question, so every
        # view on both sides has them. Lillecarl/pymux#551.
        answer(socket_path, "display-message", "kept across the upgrade")
        answer(socket_path, "confirm-before", "-p", "Still asking? (y/n)", "display-message answered")
        # The active pane of the window the client is on stops in copy
        # mode, and stays stopped across the upgrade.
        answer(socket_path, "copy-mode")
        in_mode = answer(socket_path, "list-panes", "-a", "-F", "#{pane_id} #{pane_in_mode}")
        if " 1" not in in_mode:
            raise RuntimeError("no pane went into copy mode: %r" % in_mode)
        before = every_view(socket_path, client, take_one, room, "before")
        options_before = answer(socket_path, "show-client-options", "-t", client)

        # Where the client is when the upgrade comes. Nothing moves it
        # after, so the new build has to put it back.
        listed = answer(socket_path, "list-windows", "-a", "-F", "#{session_name}:#{window_index} #{window_id}")
        windows = dict(line.split() for line in listed.splitlines())
        answer(socket_path, "switch-client", "-c", client, "-t", windows["test:2"])
        (room / LANDING).mkdir(exist_ok=True)
        before[LANDING] = steady(take_one, room / LANDING / "before.png")

        run_cli(socket_path, ["upgrade-server", shlex.join([sys.executable, "-m", "pymux"])])
        until(
            "the client back on the new build",
            lambda: answer(socket_path, "list-clients", "-F", "#{client_name}").split() == [client],
        )
        if ended() is not None:
            raise RuntimeError("the terminal closed across the upgrade")
        if answer(socket_path, "display-message", "-p", "#{pid}") == pid:
            raise RuntimeError("the old server still answers, so nothing was handed over")
        if what_the_jobs_say(socket_path) != jobs:
            raise RuntimeError("the jobs answer differently after the upgrade")
        if answer(socket_path, "list-panes", "-a", "-F", "#{pane_id} #{pane_in_mode}") != in_mode:
            raise RuntimeError("copy mode did not come back on the same pane")
        landed = steady(take_one, room / LANDING / "after.png")
        # The pass starts where the first one did, so that each window
        # marks the same previous window on both sides.
        answer(socket_path, "switch-client", "-c", client, "-t", windows[START])
        after = every_view(socket_path, client, take_one, room, "after")
        after[LANDING] = landed
        options_after = answer(socket_path, "show-client-options", "-t", client)
        (room / "client-options.txt").write_text("before:\n%s\nafter:\n%s\n" % (options_before, options_after))

        changed = []
        if options_after != options_before:
            changed.append("the client's options (client-options.txt)")
        for view, picture in before.items():
            pixels, where = difference_of(picture, after[view], picture.parent / "difference.png")
            print("%s %s: %s" % (terminal.name, view, "the same" if not pixels else where), flush=True)
            if pixels:
                changed.append("%s (%s)" % (view, where))
        if changed:
            raise RuntimeError("these views changed across the upgrade: %s" % "; ".join(changed))
        return len(before)

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

    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-upgrade"
    work.mkdir(parents=True, exist_ok=True)
    PICTURES.mkdir(parents=True, exist_ok=True)

    try:
        seats = open_the_seats(terminals, work)
    except TheSeatIsGone as reason:
        return with_no_answer(str(reason))

    failed = []
    try:
        for terminal in terminals:
            try:
                views = upgrade_in(terminal, seats[terminal.seat], work, PICTURES)
            except RuntimeError as reason:
                try:
                    seats[terminal.seat].still_there()
                except TheSeatIsGone as gone:
                    return with_no_answer(str(gone))
                failed.append(terminal.name)
                print("%s: %s\n%s" % (terminal.name, reason, every_log(PICTURES / terminal.name)), flush=True)
            else:
                print("%s: %d views, each the same picture after the upgrade" % (terminal.name, views), flush=True)
    except TheSeatIsGone as gone:
        return with_no_answer(str(gone))
    finally:
        for seat in seats.values():
            seat.keep_the_log(PICTURES)
            seat.stop()

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
