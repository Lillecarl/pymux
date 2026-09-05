"""
Run libvterm's own test suite with pymux in the middle, and compare the
result with a recorded list.

`checks.ptterm-vterm` plugs ptterm in where libvterm stands, and judges
our model. This goes one level further: the bytes of a test file reach a
program in a full screen pane, ptterm parses them, pymux renders, and a
real libvterm reads **what pymux emitted**. libvterm answers the
assertions.

So this measures the wire. A terminal on the other end of pymux sees
what this judge sees, and a foreign suite stays green on everything the
judge holds and our own model does not, as long as we emit it
faithfully.

`vterm_middleman.py` is the program that `t/run-test.pl -e` drives, and
it explains the fence that keeps a frame and a PUSH in step.

**A wire carries a screen, and nothing else.** That decides which files
can run here, and it is a different cut from the one `ptterm` makes:

- A file that expects libvterm's own callbacks cannot run in either
  check. The runner compares emitted lines in order, and neither
  harness emits any.
- A file that asks about **state** can run against ptterm and not here.
  `?pen` asks what style the *next* character takes, and nothing has
  been drawn with it, so nothing is on the wire to read it from.
  `?lineinfo` and the mode, margin and tab stop files are the same.
- A file that resizes cannot run here yet. A resize has to reach the pty
  of the client, and the pane and the judge are different sizes until
  the frame after it.

13 files are left, and they ask 152 questions about the screen and the
cursor. 145 of the answers agree.

## What the 7 that differ are

Two groups, and neither of them is libvterm being odd.

**A blank is a blank, whoever made it** (2). The renderer drops a
trailing space that a program wrote, so one row is shorter than the
program's row; and an empty row still costs one space, because
`get_max_column_index` answers 0 for a row that holds nothing. Nothing
on the wire says whether a blank was written or erased.
Lillecarl/pymux#61.

**The double size lines are held and not emitted** (5). ptterm takes
DECDWL and DECDHL now, and the direct plug-in reads them back
(Lillecarl/pymux#55). Nothing puts them on the wire, so a real libvterm
reading what pymux emitted sees a flat line. The attribute belongs to a
line, and prompt_toolkit hands the renderer fragments, which is the
same wall Lillecarl/pymux#61 hits. Lillecarl/pymux#65.

## What it has already found and fixed

**A border nobody could see made every row full width** (1 assertion).
pymux puts the highlight border of the active pane one column outside
the pane, which is the border column between two panes and off the
screen when the pane touches the edge. `Screen` keeps what it is given,
so every row held a styled cell at column 80, `get_max_column_index`
answered 80 for all of them, and no row was ever short enough to erase.
An erase of three cells went out as three spaces. prompt_toolkit's
`get_max_column_index` counts only the columns of the screen now.
Lillecarl/pymux#66.

**A cursor waiting to wrap moved the whole pane** (29 assertions). A
character in the last column leaves ptterm's cursor one column further,
where it waits. `ptterm/terminal.py` reported that column to
prompt_toolkit, and it is not on the line, so the window scrolled
sideways to bring it into view and every row of the pane was drawn one
column to the left for as long as the scroll lasted. Nothing else could
see it: the pane's own screen was right, and only a terminal reading our
wire disagreed. Lillecarl/pymux#62.

Run it:

    nix build --file . checks.pymux-vterm

Narrow it to one file while hunting one failure:

    PYMUX_VTERM_INCLUDE=unicode nix build --file . checks.pymux-vterm

Every run writes the list it saw and the log that says why:

    nix build --file . checks.pymux-vterm.run
    less result/vterm.log
    cp result/failures.txt pymux/tests/vterm-failures.txt
"""
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent

#: The program that puts pymux between the suite and libvterm.
MIDDLEMAN = HERE / "vterm_middleman.py"

#: The assertions that fail today, one per line.
BASELINE = HERE / "vterm-failures.txt"

#: How long one test file may take, in seconds. Every PUSH waits for a
#: fence and then settles, and a file holds hundreds of them, so this is
#: far above the conformance checks and still only catches a hang.
FILE_TIMEOUT = 900

#: The test files that cannot run with pymux in the middle, and the
#: reason for each.
#:
#: Two guards keep the list honest. A pattern that matches no file fails
#: the check, and a file that is left out and also named in the recorded
#: list fails the check.
NOT_OURS = (
    (
        r"^(10state_putglyph|14state_encoding|16state_resize|20state_wrapping"
        r"|21state_tabstops|28state_dbl_wh|31state_rep|12state_scroll"
        r"|13state_edit|15state_mode|27state_reset|60screen_ascii"
        r"|62screen_damage|63screen_resize|69screen_sb_clear)\.test$",
        "the file is a list of libvterm's own callbacks: every glyph it "
        "laid down, and every rectangle it damaged, scrolled or moved. "
        "The runner compares emitted lines in order and neither harness "
        "emits any, so this is left out of both checks.",
    ),
    (
        r"^(02parser|29state_fallback|03encoding_utf8)\.test$",
        "the file reads the parser and the encoder of libvterm, which "
        "is neither our model nor our wire.",
    ),
    (
        r"^(17state_mouse|18state_termprops|22state_save|25state_input"
        r"|26state_query|64screen_pen|68screen_termprops|40state_selection"
        r"|92lp1640917)\.test$",
        "the file reads what libvterm writes back to the program. pymux "
        "answers a program itself and does not pass the answer on, so "
        "the wire never carries it.",
    ),
    (
        r"^(30state_pen|32state_flow)\.test$",
        "the file asks about state that a wire does not carry. \"?pen\" "
        "is the style the next character will take, and nothing has "
        "been drawn with it yet; \"?lineinfo\" is whether a line "
        "continues the one above. Both are real questions, and "
        "checks.ptterm-vterm is where they are asked.",
    ),
    (
        r"^69screen_reflow\.test$",
        "the file resizes. A resize has to reach the pty of the client, "
        "and the pane and the judge are different sizes until the frame "
        "after it. Worth serving later; it needs a fence of its own.",
    ),
)


class Failed(AssertionError):
    pass


def read_baseline() -> Counter:
    "The assertions that are known to fail, counted."
    found: Counter = Counter()
    for line in BASELINE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found[line] += 1
    return found


def test_files(directory: Path):
    "Every test file libvterm ships, by name, in a stable order."
    return sorted(path.name for path in directory.glob("*.test"))


def left_out(name: str) -> bool:
    "True when `NOT_OURS` keeps this file from running."
    return any(re.search(pattern, name) for pattern, _ in NOT_OURS)


def run_one(directory: Path, name: str):
    "Run one test file and return what the runner printed."
    command = [
        "perl",
        str(directory / "run-test.pl"),
        "-e",
        "%s %s" % (sys.executable, MIDDLEMAN),
        str(directory / name),
    ]
    try:
        done = subprocess.run(
            command,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=FILE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise Failed("%s did not end in %d seconds" % (name, FILE_TIMEOUT))
    return done.stdout, done.stderr


_ASSERT = re.compile(r"^# line (\d+): Assert (.+) failed:$", re.MULTILINE)
_EMITTED = re.compile(r"^# line (\d+): Test failed$", re.MULTILINE)

#: What python writes when the middle man raises.
_RAISED = "Traceback (most recent call last)"


def failures_in(name: str, output: str) -> Counter:
    "The assertions of one file that failed, counted."
    found: Counter = Counter()
    for line, assertion in _ASSERT.findall(output):
        found["%s:%s %s" % (name, line, assertion)] += 1
    return found


def keep(directory: Path, failed: Counter, log: str) -> None:
    "Keep the list of failures and the log that says why."
    directory.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in sorted(failed):
        lines.extend([name] * failed[name])
    (directory / "failures.txt").write_text(
        "# The libvterm assertions that failed with pymux in the middle.\n"
        "# Each line names the test file, the line in it, and the\n"
        "# assertion. Each one is a difference between what pymux emits\n"
        "# and what the program in the pane asked for.\n"
        "#\n"
        "# ptterm/tests/vterm-failures.txt is the same list for ptterm\n"
        "# alone, judged on its own model. A name here and not there is\n"
        "# what the wire loses.\n"
        "#\n"
        "# This is what the run saw. To make it what the check expects:\n"
        "#     nix build --file . checks.pymux-vterm.run\n"
        "#     cp result/failures.txt pymux/tests/vterm-failures.txt\n"
        + "".join(line + "\n" for line in lines)
    )
    (directory / "vterm.log").write_text(log)


def report(failed: Counter, include: str) -> int:
    "Compare the run with the recorded list. Returns the exit status."
    known = read_baseline()
    chosen = Counter(
        {
            entry: count
            for entry, count in known.items()
            if re.search(include, entry.split(":")[0])
        }
    )

    new = sorted((failed - chosen).elements())
    fixed = sorted((chosen - failed).elements())

    for entry in new:
        print("vterm: FAILS NOW, and did not before: " + entry)
    for entry in fixed:
        print("vterm: PASSES NOW, so the list is out of date: " + entry)

    if new or fixed:
        print(
            "\nvterm: %s no longer describes the run. Write it again with:\n"
            "    nix build --file . checks.pymux-vterm.run\n"
            "    cp result/failures.txt pymux/tests/%s\n"
            "and read result/vterm.log for what each answer was."
            % (BASELINE.name, BASELINE.name)
        )
        return 1

    print("vterm: the run matches %s." % BASELINE.name)
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
        print("vterm: left out %s," % (", ".join(matched) or "nothing"))
        print("vterm:     because %s" % reason)
        if not matched:
            print("vterm: NOT_OURS leaves out %r, and no file has that name."
                  % pattern)
            status = 1

    both = sorted({entry.split(":")[0] for entry in known} & set(out))
    for name in both:
        print("vterm: left out, and named in the list as well: " + name)
        status = 1

    if status:
        print("\nvterm: NOT_OURS in %s no longer describes the suite."
              % Path(__file__).name)
    return status


def main() -> int:
    directory = os.environ.get("PYMUX_VTERM", "")
    if not directory:
        print("vterm: PYMUX_VTERM is not set, so there is nothing to run.")
        return 0

    suite = Path(directory)
    include = os.environ.get("PYMUX_VTERM_INCLUDE", ".*")

    names = test_files(suite)
    if not names:
        raise Failed("no test file in %s" % suite)

    failed: Counter = Counter()
    ran = []
    pieces = []
    broken = []

    for name in names:
        if left_out(name) or not re.search(include, name):
            continue
        output, errors = run_one(suite, name)
        ran.append(name)
        failed += failures_in(name, output)
        pieces.append("=== %s ===\n%s" % (name, output))
        if errors.strip():
            pieces.append("--- what the run wrote to stderr ---\n" + errors)
            if _RAISED in errors:
                broken.append(name)
        if _EMITTED.search(output):
            broken.append(name)

    log = "\n".join(pieces)

    out = os.environ.get("PYMUX_VTERM_OUT", "")
    if out:
        keep(Path(out), failed, log)

    total = sum(failed.values())
    print("vterm: %d files ran, %d assertions failed" % (len(ran), total))

    if not ran:
        print("vterm: no file ran at all")
        return 1

    status = check_the_exclusions(names, include)

    for name in sorted(set(broken)):
        print("vterm: %s expected lines the middle man cannot emit, or it "
              "raised. Read the log: this is a fault here and not a "
              "difference." % name)
        status = 1

    return report(failed, include) or status


if __name__ == "__main__":
    sys.exit(main())
