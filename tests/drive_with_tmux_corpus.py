"""
Read real tmux configuration files with pymux, and compare what fails with a recorded list.

`pymux --tmux-compat` starts on a person's tmux configuration. This says
how much of one pymux honours. `nix/tmux-corpus.nix` pins the files: tmux's
own example, oh-my-tmux, and well known dotfiles. Each goes through
`source-file` on a serving server, the way startup reads one, and every
line that fails is written down with the reason.

`tmux-corpus-failures.txt` records them. A difference in either
direction fails the check, as `drive_with_esctest.py` does: a line that
starts failing is a regression, and a line that starts passing means the
list is out of date. The log ranks the reasons by how many files hit
each, which is the work list. Lillecarl/pyterm#1027.

Run it, and write the list again:

    nix build --file . checks.pymux-tmux-corpus.run
    less result/tmux-corpus.log
    cp result/failures.txt pymux/tests/tmux-corpus-failures.txt

**The files run their `run-shell` and `if-shell` lines.** They are
downloaded, so they run only inside the build sandbox, with a fresh
`$HOME` each. A branch on `uname` reads differently on macOS, so the
list is Linux's, and the check does not judge another system.

Two variables reach this file from `pymux/nix/checks.nix`:
`PYMUX_TMUX_CORPUS` names the directory of files, and
`PYMUX_TMUX_CORPUS_OUT` the directory to write the list and the log to.
"""

from __future__ import annotations

import collections
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).parent.parent))

from pymux.commands import handle_command
from pymux.core.main import Pymux

RECORDED = Path(__file__).parent / "tmux-corpus-failures.txt"

#: One file's reading, `run-shell` lines included. The slowest takes
#: under two seconds.
SECONDS_PER_FILE = 30


async def failures_of(path: Path) -> list[str]:
    "Every line of one file that pymux refused, as `<file> line <n>: <why>`."
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        mux = Pymux()
        async with mux.running():
            await mux.panes.create_window("%s -c pass" % (sys.executable,))
            with anyio.fail_after(SECONDS_PER_FILE):
                answer = handle_command(mux, "source-file %s" % shlex.quote(str(path)))
                if answer is not None:
                    await answer
            errors = list(mux.startup_errors)
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()
    return [_one_line(error, path, home) for error in errors]


def _one_line(error: str, path: Path, home: str) -> str:
    "An error as one line that names the file by its name, not its store path."
    error = error.replace(str(path.parent) + "/", "").replace(home, "$HOME")
    # An object's address differs between runs, and the list must not.
    error = re.sub(r" at 0x[0-9a-f]+", "", error)
    return " ".join(error.split())


def reason(failure: str) -> str:
    "What a failure is about, without the file, the line or the usage."
    reason = re.sub(r"^\S+ line \d+: (pymux: )?", "", failure)
    return re.sub(r" \(pymux \S+ .*\)$", "", reason)


async def main() -> int:
    corpus = os.environ.get("PYMUX_TMUX_CORPUS")
    if not corpus:
        print("PYMUX_TMUX_CORPUS is not set; nothing to read.")
        return 0
    out = Path(os.environ.get("PYMUX_TMUX_CORPUS_OUT", "."))
    out.mkdir(parents=True, exist_ok=True)

    found: list[str] = []
    for path in sorted(Path(corpus).glob("*.conf")):
        found.extend(await failures_of(path))

    (out / "failures.txt").write_text("".join(line + "\n" for line in found))

    files = collections.Counter(failure.split(" line ")[0] for failure in found)
    reasons = collections.Counter()
    for kind in {(failure.split(" line ")[0], reason(failure)) for failure in found}:
        reasons[kind[1]] += 1

    log = ["Lines refused, per file:"]
    log += ["%5i  %s" % (n, name) for name, n in sorted(files.items())]
    log += ["", "Reasons, by how many files hit each:"]
    log += ["%5i  %s" % (n, why) for why, n in reasons.most_common()]

    if sys.platform != "linux":
        log += ["", "Not judged: the recorded list is Linux's."]
        verdict = 0
    else:
        recorded = RECORDED.read_text().splitlines() if RECORDED.exists() else []
        new = sorted(set(found) - set(recorded))
        gone = sorted(set(recorded) - set(found))
        log += [""]
        log += ["Fails now and is not in the list: %s" % line for line in new]
        log += ["In the list and passes now: %s" % line for line in gone]
        verdict = 1 if new or gone else 0
        log += ["%i lines refused, as recorded." % len(found) if not verdict else "The list is out of date."]

    text = "\n".join(log) + "\n"
    (out / "tmux-corpus.log").write_text(text)
    print(text)
    return verdict


if __name__ == "__main__":
    sys.exit(anyio.run(main))
