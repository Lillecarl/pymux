"""
Hold what images cost to a budget. Lillecarl/pymux#604.

`checks.pymux-frame` and `checks.pymux-keystroke` count text frames and
keys. Images take their own paths: pyte reads a transmission and keeps
the cells under a placement up to date, and pymux crops, scales and
re-encodes each image for each client's terminal on each frame.
Lillecarl/pymux#574 and #575 moved that work into Rust and off the
event loop, and nothing held the result.

The unit is bytecode instructions. `tests/instructions.py` says why,
and what moves a count that is not a change in the code.

## Two runs of every measurement

**(pure)** counts the Python that `PYTERM_PURE=1` keeps: every loop
over pixels is in the count. **(rust)** installs the kernels first,
as `pymux.core.main` does. A kernel runs no bytecode, so this count is the
Python around the kernels and nothing of the kernels themselves: it
says when glue grows, or when a path stops reaching its kernel. A
(rust) count near its (pure) one is a kernel nobody calls.

## The images

Three of the yazi fixtures in `tests/scenario-fixtures/images/`, as
PNG, which is what a program sends kitty: an icon, a square photograph
and a panorama much wider than the pane. The check converts them at
build time, so the counted work never decodes a JPEG.

## What each image measures

- **kitty transmission**: pyte reads the program's transmission, in
  4096-byte chunks, and places it in a box of the pane.
- **sixel transmission**: pyte reads the same picture as sixel.
- **text over it**: a screen of text drawn while the placement holds.
  Each write asks the placement which cells it covers
  (Lillecarl/pymux#598); **text alone** is the same without one.
- **sixel frame** and **kitty frame**: a client's first frame of the
  pane, which encodes or transmits the image. **again** is the next
  frame, which should cost almost nothing. **scrolled** is a frame
  after the pane scrolled one row, which crops a different piece.

    nix build --file . checks.pymux-images.run
    less result/log
    cp result/image-budgets.txt pymux/tests/image-budgets.txt

    PYMUX_IMAGES_INCLUDE=panorama nix build --file . checks.pymux-images
    PYMUX_IMAGES_TOLERANCE=2 nix build --file . checks.pymux-images
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).parent.parent))

from instructions import count_instructions
from pyte.screen import Screen
from pyte.sequences.strings import apc
from pyte.streams import Stream
from recorded import how_to_record, moved, read_counts, write_list

from pymux.graphics import kernels, sixel
from pymux.graphics.graphics import ClientGraphics, PaneView
from pymux.graphics.placements import visible_placements

HERE = Path(__file__).parent

#: The counts this check expects, one measurement per line.
BUDGETS = HERE / "image-budgets.txt"

#: How far a count may move from its budget, in percent.
DEFAULT_TOLERANCE = 5.0

ROWS = 24
COLUMNS = 80

#: The cell the client says it has, in pixels.
CELL = (10, 20)

#: The most a program puts in one kitty command, as kitty documents.
CHUNK = 4096

#: The box a program fits each image into, in cells: half the pane.
BOX = (40, 12)

#: Each fixture, by the name its measurements carry.
IMAGES = {
    "icon": "puppy-icon.png",
    "square": "pug.png",
    "panorama": "river-panorama.png",
}

#: A screen of text from the top left, over whatever the pane shows,
#: and no newline after the last row: that would scroll.
TEXT = "\x1b[H" + "\r\n".join(("row %d " % row * 12)[:COLUMNS] for row in range(ROWS))

#: One row up: the image's top row leaves, and a frame crops the rest.
SCROLL_ONE_ROW = "\x1b[%d;1H\n" % ROWS


def png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def fitted(width: int, height: int) -> tuple[int, int]:
    "The cells of `BOX` that an image fills, keeping its shape."
    columns, rows = BOX
    if width * rows * CELL[1] > height * columns * CELL[0]:
        return columns, max(1, round(columns * CELL[0] * height / width / CELL[1]))
    return max(1, round(rows * CELL[1] * width / height / CELL[0])), rows


def kitty_transmission(png: bytes) -> str:
    "What a program writes to show a PNG in a box of cells, chunked."
    columns, rows = fitted(*png_size(png))
    encoded = base64.b64encode(png).decode()
    chunks = [encoded[at : at + CHUNK] for at in range(0, len(encoded), CHUNK)] or [""]
    first = "a=T,f=100,i=1,q=2,c=%d,r=%d,m=%d" % (columns, rows, int(len(chunks) > 1))
    commands = [apc("G" + first + ";" + chunks[0])]
    for number, chunk in enumerate(chunks[1:], start=2):
        commands.append(apc("Gm=%d;%s" % (int(number < len(chunks)), chunk)))
    return "".join(commands)


def sixel_transmission(png: bytes) -> str:
    "The same picture as sixel, at the pixels of its box."
    columns, rows = fitted(*png_size(png))
    width, height = png_size(png)
    rgba = sixel.to_rgba(100, width, height, png)
    target = (columns * CELL[0], rows * CELL[1])
    scaled = sixel.scale_rgba(rgba, width, height, *target)
    return sixel.encode_sixel(*target, scaled)


def screen() -> tuple[Screen, Stream]:
    pane = Screen(ROWS, COLUMNS, write_process_input=lambda data: None)
    return pane, Stream(pane)


def shown(png: bytes) -> tuple[Screen, Stream]:
    "A pane that shows the image at its top left."
    pane, stream = screen()
    stream.feed(kitty_transmission(png))
    return pane, stream


def view_of(pane: Screen) -> PaneView:
    return PaneView(
        pane_id=1,
        x=0,
        y=0,
        width=COLUMNS,
        height=ROWS,
        vertical_scroll=pane.line_offset,
        horizontal_scroll=0,
        graphics=pane.graphics,
        screen=pane,
    )


def client(kitty: bool) -> ClientGraphics:
    graphics = ClientGraphics(lambda data: None, lambda: None)
    graphics.kitty_supported = kitty
    graphics.sixel_supported = not kitty
    graphics.cell_width, graphics.cell_height = CELL
    return graphics


def measurements(pngs: dict[str, bytes]):
    """
    Every measurement as (name, prepare), where `prepare()` builds what
    the work needs and returns the work. Building stays out of the count.
    """
    found = []

    def feeding(text: str, png: bytes | None = None):
        def prepare():
            pane, stream = shown(png) if png is not None else screen()
            return lambda: stream.feed(text)

        return prepare

    found.append(("text alone", feeding(TEXT)))

    for name, png in pngs.items():
        found.append(("%s kitty transmission" % name, feeding(kitty_transmission(png))))
        found.append(("%s sixel transmission" % name, feeding(sixel_transmission(png))))
        found.append(("%s text over it" % name, feeding(TEXT, png)))

        for protocol, kitty in (("kitty", True), ("sixel", False)):

            def first(png=png, kitty=kitty):
                pane, _stream = shown(png)
                graphics = client(kitty)
                return lambda: graphics.render([view_of(pane)])

            def again(png=png, kitty=kitty):
                pane, _stream = shown(png)
                graphics = client(kitty)
                graphics.render([view_of(pane)])
                return lambda: graphics.render([view_of(pane)])

            def scrolled(png=png, kitty=kitty):
                pane, stream = shown(png)
                graphics = client(kitty)
                graphics.render([view_of(pane)])
                stream.feed(SCROLL_ONE_ROW)
                if not any(True for _ in visible_placements(view_of(pane))):
                    raise AssertionError("the scroll took the whole image off the pane")
                return lambda: graphics.render([view_of(pane)])

            found.append(("%s %s frame" % (name, protocol), first))
            found.append(("%s %s frame again" % (name, protocol), again))
            found.append(("%s %s frame scrolled" % (name, protocol), scrolled))

    return found


HEADER = (
    "\n".join(
        [
            "# What images cost pyte and pymux, in bytecode instructions.",
            "# `tests/measure_images.py` says why the unit is not a second, what",
            "# each measurement covers, and why (rust) counts no kernel.",
            *how_to_record("pymux-images", "image-budgets.txt", "pymux/tests/image-budgets.txt"),
        ]
    )
    + "\n"
)


def main() -> int:
    include = os.environ.get("PYMUX_IMAGES_INCLUDE", "")
    tolerance = float(os.environ.get("PYMUX_IMAGES_TOLERANCE", "") or DEFAULT_TOLERANCE)
    converted = Path(os.environ["PYMUX_IMAGES_PNG"])
    pngs = {name: (converted / file).read_bytes() for name, file in IMAGES.items()}

    budgets = read_counts(BUDGETS)
    counts: dict[str, int] = {}
    wrong = []

    def judge(name: str, counted: int) -> None:
        counts[name] = counted
        budget = budgets.get(name)
        if budget is None:
            print("%-42s %12d  (no budget yet)" % (name, counted))
            wrong.append(name)
            return
        distance = moved(counted, budget)
        mark = "ok " if abs(distance) <= tolerance else "OFF"
        print("%-42s %12d  budget %12d  %+6.2f%%  %s" % (name, counted, budget, distance, mark))
        if abs(distance) > tolerance:
            wrong.append(name)

    # The pure run goes first: installing a kernel cannot be undone.
    for mode in ("pure", "rust"):
        if mode == "rust":
            kernels.install()
        for name, prepare in measurements(pngs):
            name = "%s (%s)" % (name, mode)
            if include and not re.search(include, name):
                continue
            judge(name, count_instructions(prepare()))
        print()

    if not counts:
        print("Nothing matched %r, so this run measured nothing." % include)
        return 1

    out = os.environ.get("PYMUX_IMAGES_OUT", "")
    if out:
        write_list(Path(out) / "image-budgets.txt", HEADER, counts.items())

    if include:
        print("This run measured %d of the measurements, so it makes no claim about the rest." % len(counts))

    if wrong:
        print("%d of %d moved by more than %.1f%%: %s" % (len(wrong), len(counts), tolerance, ", ".join(sorted(wrong))))
        print("A count that climbed is what this check is for. A count that fell is a budget nobody updated.")
        print("    cp result/image-budgets.txt pymux/tests/image-budgets.txt")
        return 1

    print("Every measurement is within %.1f%% of its budget." % tolerance)
    return 0


if __name__ == "__main__":
    sys.exit(main())
