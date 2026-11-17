#!/usr/bin/env agent-python
"""
How small a PNG of a text pane each model still reads without a wrong
character. Lillecarl/pyterm#1005. An experiment, not a gate.

A scratch pymux server (never the live one) shows each sample in a pane.
`capture-pane -p` is the truth, and `capture-pane -p --png --cell WxH`
the picture. A model transcribes the picture, and the score is a
character diff against the truth.

Calls shrink with the price of the model. Haiku reads every cell size.
Sonnet reads Haiku's threshold and the two sizes below it. Opus reads
Sonnet's threshold and the size below it. `--dry-run` scores the truth
against itself and calls no model.

    agent-python png_legibility.py --pymux $(nix build ... pymux)/bin/pymux --out DIR
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic

COLUMNS, ROWS = 100, 30
#: Cell widths, largest first; a cell is twice as high as it is wide.
WIDTHS = (10, 9, 8, 7, 6, 5, 4)
#: The most wrong characters per thousand that still counts as read.
THRESHOLD = 5.0

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"

PROMPT = (
    "This is a picture of a terminal screen, %d columns by %d rows. Transcribe it exactly, row by row: "
    "every character, the spacing inside each row, box drawing and wide characters as they are. "
    "Leave out trailing spaces. Answer with the rows alone, nothing before or after them."
)

LISTING = """\
total 184
drwxr-xr-x  5 carl users  4096 Nov 17 19:02 .
drwxr-xr-x 41 carl users  4096 Nov 16 08:44 ..
-rw-r--r--  1 carl users   812 Nov 17 18:59 .envrc
-rw-r--r--  1 carl users 23419 Nov 17 19:01 default.nix
-rwxr-xr-x  1 carl users  1337 Nov 12 11:20 lock-subtrees.sh
lrwxrwxrwx  1 carl users    61 Nov 17 19:02 result -> /nix/store/91ixccazy7qi545sgzi4lv37647nd14g-pymux
-rw-------  1 carl users 98304 Nov 15 23:10 snapshot-41.sqlite
drwxr-xr-x  3 carl users  4096 Nov 14 10:07 tests
-rw-r--r--  1 carl users  4410 Nov 17 18:12 README.md
"""

BOXES = """\
┌──────────┬─────────┬───────────┐
│ pane     │ size    │ command   │
├──────────┼─────────┼───────────┤
│ %1       │ 80x24   │ hx        │
│ %12      │ 120x40  │ cargo b   │
│ %307     │ 33x9    │ btop      │
└──────────┴─────────┴───────────┘
╔══╗ ╭──╮ ▏▎▍▌▋▊▉█ ░▒▓ ⠿⡇⢸ ▲▼◆●
╚══╝ ╰──╯ ←↑→↓ ⏎ ✓ ✗ ±≠≤≥ µ§¶
"""

WIDE = """\
日本語のテキスト: 端末の幅は二つ。
中文字符也占两列：终端多路复用器。
한국어 문장도 넓습니다: 터미널.
mixed: abc日本def中文ghi한국jkl
emoji: 🦀 rust, 🐍 python, 🚀 ship
narrow again: the quick brown fox.
"""


def _samples() -> dict[str, str]:
    code = "".join(Path(__file__).read_text().splitlines(keepends=True)[60:88])
    return {"code": code, "listing": LISTING, "boxes": BOXES, "wide": WIDE}


@dataclass
class Score:
    model: str
    face: str
    sample: str
    width: int
    image_tokens: int | None
    text_tokens: int | None
    characters: int
    wrong: int
    per_thousand: float
    stop: str
    seconds: float


def _normal(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines())


def wrong_characters(truth: str, read: str) -> int:
    "Characters dropped, added or changed between the two, in the larger count of each change."
    matcher = difflib.SequenceMatcher(None, truth, read, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


class Scratch:
    "A pymux server of its own, at a socket in `room`."

    def __init__(self, pymux: str, room: Path) -> None:
        # A socket's path takes 108 bytes at most, and `room` may be deeper.
        self.pymux, self.socket = pymux, Path(tempfile.mkdtemp(prefix="pl")) / "s.sock"
        self.room = room
        self.environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    def run(self, *argv: str) -> str:
        done = subprocess.run(
            [self.pymux, "-S", str(self.socket), *argv],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if done.returncode:
            raise RuntimeError("pymux %s: %s" % (shlex.join(argv), done.stderr))
        return done.stdout

    def show(self, name: str, text: str) -> str:
        path = self.room / ("%s.txt" % name)
        path.write_text(text)
        command = "sh -c %s" % shlex.quote("cat %s; exec sleep 86400" % shlex.quote(str(path)))
        self.run("new-session", "-d", "-s", name, "-x", str(COLUMNS), "-y", str(ROWS), command)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Joined, as the file holds it; the rows are what the picture holds.
            shown = self.run("capture-pane", "-p", "-J", "-t", "%s:" % name)
            if _normal(shown) == _normal(text):
                return _normal(self.run("capture-pane", "-p", "-t", "%s:" % name))
            time.sleep(0.1)
        raise RuntimeError("the pane of %s never showed its text:\n%s" % (name, shown))

    def picture(self, name: str, width: int, faces: list[str], fallbacks: list[str]) -> bytes:
        fonts = [part for face in faces for part in ("--font", face)]
        fonts += [part for fallback in fallbacks for part in ("--fallback", fallback)]
        cell = "%dx%d" % (width, 2 * width)
        said = self.run("capture-pane", "-p", "--png", "--cell", cell, *fonts, "-t", "%s:" % name)
        return base64.b64decode(said)

    def close(self) -> None:
        subprocess.run(
            [self.pymux, "-S", str(self.socket), "kill-server"], env=self.environment, capture_output=True, check=False
        )


def _image(png: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
    }


def read(client: anthropic.Anthropic, model: str, png: bytes) -> tuple[str, str]:
    response = client.messages.create(
        model=model,
        max_tokens=8000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [_image(png), {"type": "text", "text": PROMPT % (COLUMNS, ROWS)}]}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, response.stop_reason or ""


def tokens(client: anthropic.Anthropic | None, model: str, content: list) -> int | None:
    if client is None:
        return None
    return client.messages.count_tokens(model=model, messages=[{"role": "user", "content": content}]).input_tokens


def threshold(scores: list[Score], model: str, face: str) -> int | None:
    "The smallest width at which `model` reads every sample drawn in `face` within THRESHOLD."
    mine = [score for score in scores if score.model == model and score.face == face]
    widths = sorted({score.width for score in mine})
    good = [w for w in widths if all(s.per_thousand <= THRESHOLD for s in mine if s.width == w)]
    return min(good) if good else None


def total_wrong(scores: list[Score], model: str, face: str) -> int:
    return sum(score.wrong for score in scores if score.model == model and score.face == face)


def below(width: int | None, count: int) -> list[int]:
    if width is None:
        return list(WIDTHS[:2])
    at = WIDTHS.index(width)
    return list(WIDTHS[at : at + count])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pymux", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--fonts-json", required=True, type=Path, help="pymux-mcp's fonts.json: its fallbacks, and its face."
    )
    parser.add_argument("--dry-run", action="store_true", help="Score the truth against itself; call no model.")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    held = json.loads(args.fonts_json.read_text())
    # The system's monospace, as pymux-mcp draws first, and the face it bundles.
    faces = {"system": [], "bundled": held["faces"]}
    client = None if args.dry_run else anthropic.Anthropic()
    scratch = Scratch(args.pymux, args.out)
    scores: list[Score] = []
    try:
        truths = {name: scratch.show(name, text) for name, text in _samples().items()}

        def sweep(model: str, face: str, widths: list[int]) -> None:
            for width in widths:
                for name, truth in truths.items():
                    png = scratch.picture(name, width, faces[face], held["fallbacks"])
                    (args.out / ("%s-%s-%d.png" % (face, name, width))).write_bytes(png)
                    started = time.monotonic()
                    text, stop = (truth, "dry-run") if client is None else read(client, model, png)
                    (args.out / ("%s-%s-%s-%d.txt" % (model, face, name, width))).write_text(text)
                    wrong = wrong_characters(truth, _normal(text))
                    score = Score(
                        model,
                        face,
                        name,
                        width,
                        tokens(client, model, [_image(png)]),
                        tokens(client, model, [{"type": "text", "text": truth}]),
                        len(truth),
                        wrong,
                        1000 * wrong / max(1, len(truth)),
                        stop,
                        time.monotonic() - started,
                    )
                    scores.append(score)
                    print(json.dumps(asdict(score)), file=sys.stderr, flush=True)

        for face in faces:
            sweep(HAIKU, face, list(WIDTHS))
        # The dearer models read only the face Haiku read better.
        face = min(faces, key=lambda face: ((threshold(scores, HAIKU, face) or 99), total_wrong(scores, HAIKU, face)))
        sweep(SONNET, face, below(threshold(scores, HAIKU, face), 3))
        sweep(OPUS, face, below(threshold(scores, SONNET, face), 2))
    finally:
        scratch.close()
        (args.out / "scores.json").write_text(json.dumps([asdict(score) for score in scores], indent=1))
    found = {"%s %s" % (m, f): threshold(scores, m, f) for m in (HAIKU, SONNET, OPUS) for f in faces}
    print(json.dumps({key: width for key, width in found.items() if width is not None or HAIKU in key}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
