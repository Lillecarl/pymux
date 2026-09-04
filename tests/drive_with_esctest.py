"""
Run esctest2 inside a pane and compare the result with a recorded list.

esctest2 is the conformance suite that Thomas Dickey maintains, after
George Nachman wrote it for iTerm2. It judges a terminal from the
inside: it runs as a program in that terminal, writes control sequences
and reads the reports that come back. Nothing in it knows what pymux is.

So the pane is the terminal on trial. A client of pymux attaches on a
pty of this process, the suite runs in the first pane, and every
sequence it writes goes through the emulator of that pane. What the
suite reads back is what a real program in a real pane reads.

The suite reads the screen with DECRQCRA, one cell at a time. That is
the instrument. Without an answer to it, no test that looks at the
screen can say anything at all.

Most of the suite fails, and every failure names a real difference from
xterm. `esctest-failures.txt` records them. This check judges a run
against that list and complains at a difference in either direction: a
test that starts failing is a regression, and a test that starts passing
means the list is out of date. A gate that fails for reasons everybody
already knows is a gate that everybody learns to ignore.

Run it:

    nix build --file . checks.pymux-esctest

Narrow it to one class while hunting one failure:

    PYMUX_ESCTEST_INCLUDE=BSTests nix build --file . checks.pymux-esctest

Write the list again after fixing something. The result of the build
holds the new list, and the log that says why each test failed:

    PYMUX_ESCTEST_RECORD=1 nix build --file . checks.pymux-esctest
    cp result/failures.txt pymux/tests/esctest-failures.txt

Read the reasons for one class the same way. The run is judged against
nothing, so it always succeeds and always leaves the log:

    PYMUX_ESCTEST_INCLUDE=ChangeColorTests PYMUX_ESCTEST_RECORD=1 \\
        nix build --file . checks.pymux-esctest
    less result/esctest.log

Three variables reach this file from `pymux/default.nix`:
`PYMUX_ESCTEST` names the directory that holds `esctest.py`, and the
check does nothing when it is not set. `PYMUX_ESCTEST_INCLUDE` is the
regular expression of test names to run. `PYMUX_ESCTEST_RECORD` names
the directory to write the list and the log into.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.drive_with_pty import Failed, Terminal, run_cli  # noqa: E402

HERE = Path(__file__).parent

#: The tests that fail today, one name per line.
BASELINE = HERE / "esctest-failures.txt"

#: The suite asks for a screen of 25 by 80 with XTWINOPS, and a pane
#: cannot resize its window, so the pty of the client has to give it
#: that. A pane takes one row for its title, and the session one for
#: the status line.
ROWS, COLUMNS = 27, 80

#: How long a report may take before the suite gives up on it, in
#: seconds. A sequence that goes unanswered costs this much every time
#: a test asks for it.
REPORT_TIMEOUT = "2"

#: How long the whole run may take.
RUN_TIMEOUT = 900.0

#: What the runner writes when the suite is done.
FINISHED = b"ESCTEST-FINISHED"

#: The program of the pane. It runs the suite and then waits, because a
#: pane that ends takes the session with it and there would be nothing
#: left to read.
#:
#: It drives the suite test by test instead of letting it run itself,
#: for one reason. A test that reads fewer reports than the terminal
#: sends leaves the rest in the pipe, and every read after that is one
#: report behind. One deviation early on then decides the result of
#: every test after it: 514 of 568 tests failed that way, against 348
#: when each starts clean. Throwing away what is left over before each
#: test keeps every verdict about that test alone.
RUNNER = '''
import os, select, sys, tty

# Nobody reads the pane after this, and a traceback drawn on it goes
# away with the pane. Put it where the check can find it.
sys.stderr = open(os.environ["ESCTEST_LOG"] + ".stderr", "w", buffering=1)

sys.path.insert(0, os.environ["ESCTEST_DIR"])
os.chdir(os.environ["ESCTEST_DIR"])

# esctest.py runs itself when it is imported: the last line of the file
# calls main(). So the import is the run, and the arguments have to be
# in place before it. "^$" matches no test, which makes that run empty
# and leaves the suite set up and ready to be driven.
sys.argv = ["esctest.py",
            "--expected-terminal=xterm",
            # Which xterm ptterm answers as, where the two differ.
            # xterm 383 split reverse wraparound in two: "?45" now
            # goes back only over a line that was reached by wrapping,
            # and "?1045" carries the old behaviour that went back
            # over any line. ptterm follows the split, so the suite has
            # to ask for the later reading.
            "--xterm-reverse-wrap=383",
            "--no-print-logs",
            "--logfile=" + os.environ["ESCTEST_LOG"],
            "--timeout=" + os.environ["ESCTEST_TIMEOUT"],
            "--include=^$"]

import escargs, escio, esclog, esctest

if escio.stdin_fd is None:
    # The import ran nothing, so the file has grown a main guard since.
    esctest.init()

# That empty run ended with a shutdown, which takes the terminal out of
# raw mode. And now the real selection of tests.
tty.setraw(escio.stdin_fd)
escargs.args.include = os.environ["ESCTEST_INCLUDE"]


def drain():
    "Throw away every report that the test before this one left."
    while select.select([0], [], [], 0.05)[0]:
        if not os.read(0, 65536):
            break


passed = failed = known = 0
try:
    for name, method in esctest.MatchingNamesAndMethods():
        drain()
        status = esctest.RunTest(name, method)
        if status is None:
            known += 1
        elif status:
            passed += 1
        else:
            failed += 1
    esclog.LogInfo("*** %d passed, %d known bugs, %d failed ***"
                   % (passed, known, failed))
finally:
    escio.Shutdown()

# The last test leaves the screen wherever it left it: margins, origin
# mode, an alternate screen. Put it back before writing anything, or the
# line below lands somewhere nobody reads.
sys.stdout.write("\\x1bc\\r\\n%s-%d\\r\\n" % ("ESCTEST-FINISHED", failed))
sys.stdout.flush()
while sys.stdin.read(1):
    pass
'''


def read_baseline():
    "The tests that are known to fail, as a set of names."
    names = set()
    for line in BASELINE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(line)
    return names


def write_record(directory: Path, failed, log: str) -> None:
    """
    Keep the list of failures and the log that says why.

    The log matters as much as the list. A run happens in the build
    sandbox, so without this the reasons go away with it, and the names
    alone do not say what a pane did wrong.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "failures.txt").write_text(
        "# The esctest2 tests that fail today. Every name here is a real\n"
        "# difference between a pymux pane and xterm.\n"
        "#\n"
        "# Write this file again with:\n"
        "#     PYMUX_ESCTEST_RECORD=1 nix build --file . checks.pymux-esctest\n"
        "#     cp result/failures.txt pymux/tests/esctest-failures.txt\n"
        + "".join(name + "\n" for name in sorted(failed))
    )
    (directory / "esctest.log").write_text(log)


def failures_in(log: str):
    """
    The tests that failed, as a set of names.

    The suite writes one line per failure while it runs. Those are read
    rather than the list it prints at the end, because a run that dies
    halfway still wrote them.
    """
    return set(re.findall(r"^\*\*\* TEST (\S+) FAILED:", log, re.MULTILINE))


def tests_that_ran(log: str):
    "Every test the suite started, as a set of names."
    return set(re.findall(r"^Run test: (\S+)$", log, re.MULTILINE))


def run(tmp: Path, directory: Path) -> str:
    "Run the suite in a pane and return what it logged."
    runner = tmp / "esctest_runner.py"
    runner.write_text(RUNNER)

    log = tmp / "esctest.log"
    os.environ["ESCTEST_DIR"] = str(directory)
    os.environ["ESCTEST_LOG"] = str(log)
    os.environ["ESCTEST_TIMEOUT"] = REPORT_TIMEOUT
    os.environ["ESCTEST_INCLUDE"] = os.environ.get("PYMUX_ESCTEST_INCLUDE", ".*")

    terminal = Terminal(
        tmp,
        "esctest",
        command="%s %s" % (sys.executable, runner),
        rows=ROWS,
        columns=COLUMNS,
    )
    try:
        # Answer the detection of the client the way a capable terminal
        # does. A client that is still waiting draws nothing.
        terminal.wait_for_the_queries()
        terminal.write(b"\x1b[?31u")  # Keyboard flags.
        terminal.write(b"\x1b_Gi=31;OK\x1b\\")  # Kitty graphics.
        terminal.write(b"\x1b[6;20;10t")  # Cell size.
        terminal.write(b"\x1b[?62;1;6c")  # Device attributes.

        # The pane has to be the size the suite asks for, or every test
        # that counts columns means nothing.
        listed = run_cli(
            terminal.sock_path, ["list-panes", "-F", "#{pane_height}x#{pane_width}"]
        )
        assert listed.returncode == 0, listed.stderr
        size = listed.stdout.decode().strip()
        if size != "25x80":
            raise Failed("the pane is %s, and the suite wants 25x80" % size)

        terminal.wait_for(FINISHED, timeout=RUN_TIMEOUT)
    except BaseException:
        terminal.report()
        stderr = Path(str(log) + ".stderr")
        if stderr.exists():
            print("--- what the pane wrote to stderr ---")
            print(stderr.read_text(errors="replace")[-4000:])
        if log.exists():
            print("--- the last of the esctest log ---")
            print(log.read_text(errors="replace")[-4000:])
        raise
    finally:
        terminal.close()

    return log.read_text(errors="replace")


def report(log: str) -> int:
    """
    Compare the run with the recorded list. Returns the exit status.

    A difference in either direction is a failure, and the message says
    how to record the list again.
    """
    ran = tests_that_ran(log)
    failed = failures_in(log)
    known = read_baseline()

    print("esctest: %d tests ran, %d failed" % (len(ran), len(failed)))

    if not ran:
        print("esctest: the suite ran nothing at all")
        return 1

    new = sorted(failed - known)
    fixed = sorted((known & ran) - failed)
    missing = sorted(known - ran)

    for name in new:
        print("esctest: FAILS NOW, and did not before: " + name)
    for name in fixed:
        print("esctest: PASSES NOW, so the list is out of date: " + name)
    for name in missing:
        print("esctest: named in the list, but the suite never ran it: " + name)

    if new or fixed or missing:
        print(
            "\nesctest: %s no longer describes the run. Write it again with:\n"
            "    PYMUX_ESCTEST_RECORD=1 nix build --file . checks.pymux-esctest\n"
            "    cp result/failures.txt pymux/tests/%s\n"
            "and read result/esctest.log for what each one did."
            % (BASELINE.name, BASELINE.name)
        )
        return 1

    print("esctest: the run matches %s." % BASELINE.name)
    return 0


def main() -> int:
    directory = os.environ.get("PYMUX_ESCTEST", "")
    if not directory:
        print("esctest: PYMUX_ESCTEST is not set, so there is nothing to run.")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="pymux-esctest-"))
    log = run(tmp, Path(directory))

    record = os.environ.get("PYMUX_ESCTEST_RECORD", "")
    if record:
        failed = failures_in(log)
        write_record(Path(record), failed, log)
        print("esctest: wrote %d names and the log to %s" % (len(failed), record))
        return 0

    return report(log)


if __name__ == "__main__":
    sys.exit(main())
