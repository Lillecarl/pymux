"""
Unmodified TUI applications, driven step by step, each shown at once by
two terminals of one kind: one bare, one through pymux. Lillecarl/pymux#593.

A scenario is steps: keys, a wait until some text is on the screen, and
a picture of both terminals. The waits read the pyte screen the proxy
feeds in this process, so they cost no picture; `Seat.twins` says how
the two terminals see one run.

Each picture pair is compared, and the run is judged against
`scenario-differences.txt` the way the still pictures are judged
against theirs: a difference either way fails.

    PYMUX_SCENARIOS=yazi nix build --file . checks.pymux-scenarios.run

`PYMUX_SCENARIOS` and `PYMUX_SCENARIO_TERMINALS` narrow the run to the
scenarios and terminals whose name holds them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from pyterm_pytest.seats import TheSeatIsGone, differences, open_the_seats, with_no_answer
from take_picture import PICTURE_CONFIG, TERMINALS, every_log, pymux_command

OUT = Path(os.environ.get("PYMUX_SCENARIOS_OUT", "scenario-pictures"))
ONLY = os.environ.get("PYMUX_SCENARIOS", "")
ONLY_TERMINALS = os.environ.get("PYMUX_SCENARIO_TERMINALS", "")
RECORDED = Path(__file__).parent / "scenario-differences.txt"
FIXTURES = Path(__file__).parent / "scenario-fixtures"


class Yazi:
    "The file manager, on a tree of its own."

    command = "exec yazi"

    def prepare(self, room):
        tree = room / "tree"
        (tree / "alpha").mkdir(parents=True)
        (tree / "alpha" / "inside-alpha").write_text("")
        (tree / "beta").mkdir()
        (tree / "notes.txt").write_text("a note to preview\n")
        (tree / "readme.md").write_text("the readme\n")
        return {"HOME": str(room / "home")}, tree

    def steps(self, twins, photograph):
        # The first entry is hovered, and its preview lists what it holds.
        twins.shows("inside-alpha")
        photograph("start")
        twins.press("G")
        twins.shows("the readme")
        photograph("bottom")
        twins.press("q")
        twins.until_it_ends()


class YaziImages:
    """
    The file manager previewing photographs: one of each format and
    shape in `scenario-fixtures/images`, hovered in turn.
    """

    command = "exec yazi"
    # yazi picks an image protocol and sizes a picture by what the
    # terminal answers, so each terminal runs a yazi of its own and
    # answers it, as it would for a person.
    apart = True
    images = sorted(path for path in (FIXTURES / "images").iterdir() if path.suffix != ".md")

    def prepare(self, room):
        tree = room / "images"
        tree.mkdir()
        for image in self.images:
            shutil.copy(image, tree / image.name)
        return {"HOME": str(room / "home")}, tree

    def steps(self, twins, photograph):
        for number, image in enumerate(sorted(self.images, key=lambda path: path.name.lower())):
            if number:
                twins.press("j")
            # The status bar names the hovered file on the last row.
            twins.until(lambda rows: image.name in rows[-1], "%s was never hovered" % image.name)
            photograph(image.stem)
        twins.press("q")
        twins.until_it_ends()


class ClaudeCode:
    """
    claude-code, against a model API that answers from a fixture.

    `hasCompletedOnboarding` and the trusted folder skip the two
    questions a first start asks.
    """

    command = "exec claude"

    def __init__(self):
        self._mock = None

    def prepare(self, room):
        port = _a_free_port()
        self._mock = subprocess.Popen(
            ["llmock", "-p", str(port), "-f", str(FIXTURES), "--log-level", "warn"],
            stdout=open(room / "llmock.log", "wb"),
            stderr=subprocess.STDOUT,
        )
        _until_it_listens(port, self._mock, room / "llmock.log")
        home = room / "home"
        home.mkdir()
        directory = room / "project"
        directory.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": {str(directory): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        return {
            "HOME": str(home),
            # No "/v1": Claude Code appends "/v1/messages" itself.
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % port,
            "ANTHROPIC_AUTH_TOKEN": "mock",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": "claude-sonnet-4-6",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }, directory

    def steps(self, twins, photograph):
        twins.shows("for shortcuts")
        photograph("start")
        twins.press("hello")
        twins.shows("hello")
        twins.press("\r")
        twins.shows("Hello from aimock")
        photograph("answer")

    def finish(self):
        if self._mock is not None:
            self._mock.terminate()
            self._mock.wait(timeout=5)


SCENARIOS = {"yazi": Yazi, "yazi-images": YaziImages, "claude-code": ClaudeCode}


def _a_free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _until_it_listens(port, process, log, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("llmock ended with %s\n%s" % (process.returncode, log.read_text()))
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("llmock never listened on %d\n%s" % (port, log.read_text()))


def run_one(terminal, seat, name, work, out):
    "Drive one scenario in one terminal, and give back {step: pixels that differ}."
    room = out / terminal.name / name
    room.mkdir(parents=True, exist_ok=True)
    scene = work / ("%s-%s" % (terminal.name, name))
    scene.mkdir(parents=True, exist_ok=True)
    config_path = work / "full-screen.conf"
    config_path.write_text(PICTURE_CONFIG)
    scenario = SCENARIOS[name]()

    def through_pymux(viewer):
        return pymux_command(
            viewer,
            work / ("%s-%s.sock" % (terminal.name, name)),
            config_path,
            room / "pymux-server.log",
            room / "pymux-stderr.log",
        )

    found = {}

    def director(twins):
        def photograph(step):
            bare, through = room / ("%s-bare.png" % step), room / ("%s-pymux.png" % step)
            twins.photograph((bare, through))
            found[step] = differences(bare, through, room / ("%s-difference.png" % step))
            print("%s %s %s: %d pixels differ" % (terminal.name, name, step, found[step]), flush=True)

        try:
            scenario.steps(twins, photograph)
        except RuntimeError:
            (room / "screen.txt").write_text("\n".join(twins.rows()) + "\n")
            raise
        finally:
            # What each application wrote, for a person to read and for a
            # still fixture to replay, and how each terminal answered it.
            twins.record(room)

    try:
        environment, directory = scenario.prepare(scene)
        seat.twins(
            terminal,
            through_pymux,
            scenario.command,
            work,
            (room / "bare.log", room / "pymux.log"),
            director,
            environment=environment,
            directory=str(directory),
            apart=getattr(scenario, "apart", False),
        )
    except RuntimeError as reason:
        raise RuntimeError("%s\n%s" % (reason, every_log(room, seat))) from None
    finally:
        getattr(scenario, "finish", lambda: None)()
    return found


def read_recorded():
    "The differences that stand, as {(terminal, scenario, step): pixels}."
    standing = {}
    if RECORDED.exists():
        for line in RECORDED.read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                terminal, scenario, step, pixels = line.split()
                standing[(terminal, scenario, step)] = int(pixels)
    return standing


def write_recorded(path, seen):
    lines = [
        "# Every difference between a scenario's picture with pymux and one without.",
        "# `tests/photograph_scenarios.py` says what this is and how to write it.",
        "",
    ]
    lines += ["%s %s %s %d" % (*key, pixels) for key, pixels in sorted(seen.items()) if pixels]
    path.write_text("\n".join(lines) + "\n")


def main():
    work = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-scenarios"
    work.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    names = [name for name in SCENARIOS if ONLY in name]
    terminals = [t for t in TERMINALS if ONLY_TERMINALS in t.name]
    if not names or not terminals:
        raise SystemExit("no scenario holds %r, or no terminal holds %r" % (ONLY, ONLY_TERMINALS))

    try:
        seats = open_the_seats(terminals, work)
    except TheSeatIsGone as reason:
        return with_no_answer(str(reason))

    seen, wrong = {}, []
    try:
        for terminal in terminals:
            for name in names:
                started = time.time()
                try:
                    for step, pixels in run_one(terminal, seats[terminal.seat], name, work, OUT).items():
                        seen[(terminal.name, name, step)] = pixels
                except RuntimeError as reason:
                    try:
                        seats[terminal.seat].still_there()
                    except TheSeatIsGone as gone:
                        return with_no_answer(str(gone))
                    wrong.append("%s %s: %s" % (terminal.name, name, reason))
                print("%s %s took %.1f s" % (terminal.name, name, time.time() - started), flush=True)
    finally:
        for seat in seats.values():
            seat.keep_the_log(OUT)
            seat.stop()

    write_recorded(OUT / "scenario-differences.txt", seen)
    standing = read_recorded()
    for key, found in sorted(seen.items()):
        expected = standing.get(key, 0)
        if found != expected:
            wrong.append("%s %s %s: %d pixels differ, %d were recorded" % (*key, found, expected))
    if wrong:
        print("\n--- pymux draws something else ---", file=sys.stderr)
        for line in wrong:
            print(line, file=sys.stderr)
        print(
            "\nLook at the difference pictures, and if a difference is right,\n"
            "take the list this run wrote and say why in a comment:\n"
            "    nix build --file . checks.pymux-scenarios.run\n"
            "    cp result/scenario-differences.txt pymux/tests/scenario-differences.txt",
            file=sys.stderr,
        )
        return 1
    print("Every scenario is what it was recorded to be.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
