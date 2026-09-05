"""
Run Alacritty's reference tests with pymux in the middle.

Alacritty records a program: the bytes it wrote, the screen it wrote
them to, and the grid that came out. `alacritty_terminal/tests/ref.rs`
replays the bytes into a `Term` and compares the grid.

This changes one thing. The bytes go to a full screen pymux pane, and
what pymux emitted is what the `Term` reads:

    the recording ──▶ a program in a full screen pane
                                │
                           ptterm parses
                                │
                       pymux renders and emits
                                │
        grid.json ◀── compared ── a real Term reads our wire

So Alacritty's own assertion holds if, and only if, we emit what the
program drew. `ptterm/tests/judges/src/bin/alacritty-ref.rs` is the
judge, and nothing of ours decides anything: the grid is built and
compared by `alacritty_terminal`.

**This is the second borrowed suite to go through the middle man, and
the first that is not libvterm's.** `checks.pymux-vterm` asks a
question at a time in a protocol libvterm wrote. This one hands over a
whole recording and reads one verdict. `tests/middleman.py` is the part
they share, which is what makes a third one cheap. Lillecarl/pymux#49.

**Why it is worth having next to the panel.** `ptterm-panel` gives the
same bytes to Alacritty and to ptterm and compares two screens, so it
measures our model against theirs. This measures our wire against a
recording of a real program: vim, tmux, fish and zsh, tens of thousands
of bytes each. Nothing else here runs a program of that size.

## What the 17 that differ are

Five groups, and each one is a question about the wire and not about
the recording.

**A colour the program named is not the colour we emit** (4 tests:
`indexed_256_colors`, `sgr`, `origin_goto`, `vim_large_window_scroll`).
Three of the four are one colour in two encodings: a program that
writes `SGR 48 ; 5 ; 1` gets `Named(Red)` from us and Alacritty
recorded `Indexed(1)`. Both paint the same cell, because the first
sixteen travel as `SGR 31` and `SGR 41`, which is what every
multiplexer emits. Alacritty keeps them apart in its model only, so
this is recorded and not fixed. The fourth is the other way round:
`vim_large_window_scroll` recorded `Spec(0,0,0)` where we emit
nothing, so a cell painted black reads as the default background.
Lillecarl/pymux#71.

**Alacritty reads "SGR 21" as the end of bold** (1 test: `underline`).
The recording writes `CSI 4:3 ; 21 m`, a curl and then 21, and records
a curl. ptterm draws a double line, which is what ECMA-48 numbers 21.
Five judges are with ptterm and Alacritty is alone, so this one is
recorded and not fixed: `ptterm/tests/DEVIATIONS.md`, "Where Alacritty
looks wrong". A bare `CSI 4:3 m` does reach the wire as `SGR 4:3`.

**The id of a hyperlink is dropped** (1 test: `hyperlinks`). The URI
survives and the id does not, so Alacritty numbers each run itself and
a link that wraps becomes two. Lillecarl/pymux#70.

**A tab is a space** (2 tests: `tab_rendering`, `vttest_tab_clear_set`).
Alacritty keeps the tab character in the cell it moved from. Nobody has
asked the panel yet whether that is a rule or Alacritty's own model.

**A cell holds something else** (9 tests: `decaln_reset`,
`deccolm_reset`, `delete_lines`, `delete_chars_reset`, `saved_cursor`,
`saved_cursor_alt`, `scroll_up_reset`, `selective_erasure`,
`wrapline_alt_toggle`). Each one
is its own question, and some are already answered elsewhere:
`deccolm_reset` needs a 132 column page, which a pane cannot take
(`ptterm/tests/DEVIATIONS.md`), and `selective_erasure` is DECSCA,
where libvterm agrees with us and Alacritty erases nothing at all.

## What it has already found and fixed

**A palette number above fifteen reached the wire as `#rrggbb`**
(2 tests). A program that asks for colour 234 asks the terminal of the
user to paint 234, and that terminal has a theme. ptterm answered out
of the table of xterm instead, so 240 colours of the theme were gone.
A number of the palette now travels as a number. Lillecarl/pymux#68.

**An erase carried the underline onto every blank it left** (3 tests).
A shell left the underline on and ran `clear`, and two thousand blank
cells came off our wire underlined. Five judges take the underline off
an erased cell and only kitty keeps it, while the background is the
other way round. `erase_style` keeps the background and reverse video
now, and drops the line. Lillecarl/pymux#67.

Three variables reach this file from `pymux/nix/checks.nix`.
`PYMUX_ALACRITTY` is the directory of reference tests and
`PYMUX_ALACRITTY_JUDGE` is the built judge; without either there is
nothing to run. `PYMUX_ALACRITTY_OUT` is where the run leaves its
report.

Three knobs narrow a run or say more about it:

    PYMUX_ALACRITTY_INCLUDE=vttest nix build --file . checks.pymux-alacritty
    PYMUX_ALACRITTY_TRACE=1 nix build --file . checks.pymux-alacritty.run
    PYMUX_ALACRITTY_WIRE=1 nix build --file . checks.pymux-alacritty.run

A narrowed run is judged too, and it makes no claim about the tests it
did not choose. `PYMUX_ALACRITTY_WIRE` keeps the bytes pymux emitted
for each test in `result/wire/<name>.bin`, which is where the sequence
behind a wrong cell is.

## What the run is judged against

`alacritty-failures.txt` names the tests that differ today, one per
line. A test that starts failing and one that starts passing both fail
the check, the same way the conformance lists work:

    nix build --file . checks.pymux-alacritty.run
    less result/alacritty.log
    cp result/failures.txt pymux/tests/alacritty-failures.txt
"""
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.middleman import Pane  # noqa: E402

HERE = Path(__file__).parent

#: The tests that differ today, one name per line.
BASELINE = HERE / "alacritty-failures.txt"

#: How long the fence of one recording may take, in seconds. The whole
#: recording goes down the fifo at once and every byte of it is parsed
#: and drawn before the fence comes back. The largest is a third of a
#: megabyte.
FENCE_TIMEOUT = 300.0

#: How long the judge may take to read one wire, in seconds.
JUDGE_TIMEOUT = 120.0

#: The reference tests that cannot run with pymux in the middle, and the
#: reason for each.
#:
#: Two guards keep the list honest. A pattern that matches no test fails
#: the check, and a test that is left out and also named in the recorded
#: list fails the check.
NOT_OURS = (
    (
        r"^(grid_reset|history|region_scroll_down|row_reset"
        r"|scroll_in_region_up_preserves_history)$",
        "the test keeps a scrollback, and the recorded grid holds the "
        "lines that scrolled away. A wire carries a screen and nothing "
        "else: pymux draws the pane, and what left the top of it was "
        "never emitted. The judge refuses these by name as well, rather "
        "than answering a question it cannot answer.",
    ),
)


class Failed(AssertionError):
    pass


def _trace(message: str) -> None:
    "Write one line of the exchange to the standard error."
    if os.environ.get("PYMUX_ALACRITTY_TRACE"):
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


def _keep_the_wire(name: str, wire: bytes) -> None:
    """
    Keep the bytes that pymux emitted, when the run is asked to.

    A difference says which cell is wrong. It does not say which
    sequence made it wrong, and the wire is the only place that
    answers. It is tens of thousands of bytes, so a run keeps it only
    when somebody asks.
    """
    out = os.environ.get("PYMUX_ALACRITTY_OUT", "")
    if not out or not os.environ.get("PYMUX_ALACRITTY_WIRE"):
        return
    wires = Path(out) / "wire"
    wires.mkdir(parents=True, exist_ok=True)
    (wires / (name + ".bin")).write_bytes(wire)


def read_baseline() -> set:
    "The tests that are known to differ."
    found = set()
    for line in BASELINE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found.add(line)
    return found


def reference_tests(directory: Path):
    "Every reference test Alacritty ships, by name, in a stable order."
    return sorted(
        path.name for path in directory.iterdir() if (path / "grid.json").is_file()
    )


def left_out(name: str) -> bool:
    "True when `NOT_OURS` keeps this test from running."
    return any(re.search(pattern, name) for pattern, _ in NOT_OURS)


def the_wire(tmp: Path, name: str, directory: Path) -> bytes:
    """
    Put one recording on the screen of a pane, and read our wire back.

    Everything pymux wrote goes to the judge, from the moment the client
    attached. The reference is a blank terminal and then the recording,
    so ours has to be a blank terminal and then our whole wire: a frame
    the judge never saw is a style it never took.
    """
    size = json.loads((directory / "size.json").read_text())
    recording = (directory / "alacritty.recording").read_bytes()

    pane = Pane(
        tmp,
        name,
        size["screen_lines"],
        size["columns"],
        trace=_trace,
        # Every colour of the recording has to survive the wire. At a
        # lower depth pymux would round each one to the palette, and
        # every colour test would fail for a reason that is not the wire.
        colorterm="truecolor",
    )
    pane.start()
    try:
        _trace(
            "%s: %d bytes onto %d by %d"
            % (name, len(recording), size["screen_lines"], size["columns"])
        )
        pane.write(recording, timeout=FENCE_TIMEOUT)
        if os.environ.get("PYMUX_ALACRITTY_TRACE"):
            pane.trace_the_pane()
        return pane.seen
    finally:
        pane.close()


def run_one(tmp: Path, judge: str, name: str, directory: Path):
    "Run one reference test. Returns whether it agreed, and what it said."
    try:
        wire = the_wire(tmp, name, directory)
    except Exception:
        return False, "the pane could not run it:\n" + traceback.format_exc()

    _trace("%s: %d bytes of wire to the judge" % (name, len(wire)))
    _keep_the_wire(name, wire)
    try:
        done = subprocess.run(
            [judge, str(directory)],
            input=wire,
            capture_output=True,
            timeout=JUDGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "the judge did not answer in %d seconds" % JUDGE_TIMEOUT

    said = done.stdout.decode("utf-8", "replace") + done.stderr.decode(
        "utf-8", "replace"
    )
    if done.returncode == 0:
        return True, said
    if done.returncode == 1:
        return False, said
    raise Failed("the judge could not read %s: %s" % (name, said.strip()))


def keep(directory: Path, differed, log: str) -> None:
    "Keep the list of tests that differ and the log that says why."
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "failures.txt").write_text(
        "# The Alacritty reference tests whose grid differs with pymux\n"
        "# in the middle. Each line is one test.\n"
        "#\n"
        "# A difference is between the grid Alacritty recorded for a\n"
        "# real program and the grid a real Term builds out of what\n"
        "# pymux emitted for the same program. So each one is something\n"
        "# our wire loses or adds.\n"
        "#\n"
        "# This is what the run saw. To make it what the check expects:\n"
        "#     nix build --file . checks.pymux-alacritty.run\n"
        "#     cp result/failures.txt pymux/tests/alacritty-failures.txt\n"
        + "".join(name + "\n" for name in sorted(differed))
    )
    (directory / "alacritty.log").write_text(log)


def report(differed, include: str) -> int:
    "Compare the run with the recorded list. Returns the exit status."
    known = {name for name in read_baseline() if re.search(include, name)}

    new = sorted(set(differed) - known)
    fixed = sorted(known - set(differed))

    for name in new:
        print("alacritty: DIFFERS NOW, and did not before: " + name)
    for name in fixed:
        print("alacritty: AGREES NOW, so the list is out of date: " + name)

    if new or fixed:
        print(
            "\nalacritty: %s no longer describes the run. Write it again with:\n"
            "    nix build --file . checks.pymux-alacritty.run\n"
            "    cp result/failures.txt pymux/tests/%s\n"
            "and read result/alacritty.log for what each cell was."
            % (BASELINE.name, BASELINE.name)
        )
        return 1

    print("alacritty: the run matches %s." % BASELINE.name)
    return 0


def check_the_exclusions(names, include: str) -> int:
    "Say whether `NOT_OURS` still describes the suite."
    if include != ".*":
        return 0

    status = 0
    known = read_baseline()
    out = [name for name in names if left_out(name)]

    for pattern, reason in NOT_OURS:
        matched = sorted(name for name in names if re.search(pattern, name))
        print("alacritty: left out %s," % (", ".join(matched) or "nothing"))
        print("alacritty:     because %s" % reason)
        if not matched:
            print(
                "alacritty: NOT_OURS leaves out %r, and no test has that name."
                % pattern
            )
            status = 1

    both = sorted(known & set(out))
    for name in both:
        print("alacritty: left out, and named in the list as well: " + name)
        status = 1

    if status:
        print(
            "\nalacritty: NOT_OURS in %s no longer describes the suite."
            % Path(__file__).name
        )
    return status


def main() -> int:
    suite = os.environ.get("PYMUX_ALACRITTY", "")
    judge = os.environ.get("PYMUX_ALACRITTY_JUDGE", "")
    if not suite or not judge:
        print("alacritty: no suite and no judge, so there is nothing to run.")
        return 0

    directory = Path(suite)
    include = os.environ.get("PYMUX_ALACRITTY_INCLUDE", ".*")

    names = reference_tests(directory)
    if not names:
        raise Failed("no reference test in %s" % directory)

    tmp = Path(os.environ.get("PYMUX_ALACRITTY_TMP", ".")) / ("run-%d" % os.getpid())
    tmp.mkdir(parents=True, exist_ok=True)

    differed = []
    ran = []
    pieces = []

    for name in names:
        if left_out(name) or not re.search(include, name):
            continue
        agreed, said = run_one(tmp, judge, name, directory / name)
        ran.append(name)
        if not agreed:
            differed.append(name)
            pieces.append("=== %s ===\n%s" % (name, said))
        elif said.strip():
            pieces.append("=== %s (agrees) ===\n%s" % (name, said))

    log = "\n".join(pieces)
    print("alacritty: ran %d of the %d tests." % (len(ran), len(names)))
    print("alacritty: %d of them differ." % len(differed))
    print(log)

    out = os.environ.get("PYMUX_ALACRITTY_OUT", "")
    if out:
        keep(Path(out), differed, log)

    if not ran:
        raise Failed("no reference test ran, and %r chose them" % include)

    return report(differed, include) | check_the_exclusions(names, include)


if __name__ == "__main__":
    sys.exit(main())
