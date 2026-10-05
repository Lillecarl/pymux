"""
A scrollable alt-screen document viewer, and the benchmark's load.

Run it in any terminal and scroll: arrows and `j`/`k` move a line,
`space`/`b` a page, `g`/`G` the ends, `q` quits. The wheel works where
the terminal sends it (SGR mouse is on). It draws like the programs it
stands in for -- `less`, `vim`, `fzf`: an alternate screen, a viewport
over a long document, a footer with the position.

Two ways of moving, because the two cost different things:

- **redraw**: every step rewrites the whole viewport. That is what
  `fzf` does on each keystroke and `vim` on most scrolls: the parser
  sees a screenful of cells and the renderer diffs one.
- **scroll**: the viewport is a scroll region and a step deletes or
  inserts one line, then draws the one that came into view. That is
  `less` moving a line: the parser scrolls and the renderer diffs a
  screen that barely changed.

`--styled` colours the line numbers and marks every seventh line, the
way a syntax-highlighted pager does. Styled cells cost the diff and
the escape writer more, so a benchmark that only scrolls plain text
measures the wrong program.

The benchmark does not run this file. It imports
`viewport_bytes` and `scroll_step` and feeds what they return into a
pane, so the bytes a profile attributes are byte for byte what the
viewer emits on a scroll step. One source of truth, two users.

Lines never wrap: each one is padded or cut to the width. A terminal
that wraps would pay the parser for the wrap; this viewer holds that
still so the width sweep measures the screen and not the rewrapping.
"""

from __future__ import annotations

import argparse
import fcntl
import signal
import struct
import sys
import termios
import tty

#: How many lines the document holds. Long enough that no realistic
#: viewport reaches an end mid-benchmark.
LINES = 5000

#: Words the document lines are built from, so the text varies in
#: length and content without carrying a data file.
WORDS = (
    "scroll",
    "viewport",
    "margin",
    "stutter",
    "frame",
    "gutter",
    "pane",
    "margin",
    "cursor",
    "region",
    "delete",
    "insert",
    "reverse",
    "index",
    "window",
    "status",
    "border",
    "title",
    "chrome",
    "screen",
)


def document_line(number: int) -> str:
    """
    Line `number` (one based) of the document, without styling.

    The text varies in length so columns differ the way prose does,
    and every eleventh line runs long on purpose: the viewer cuts it
    at the width, like a pager that does not wrap.
    """
    words = [WORDS[(number * 7 + shift) % len(WORDS)] for shift in range(3 + number % 9)]
    text = " ".join(words)
    if number % 11 == 0:
        text += " " + " ".join(WORDS)
    return text


def viewport_bytes(top: int, rows: int, columns: int, *, styled: bool) -> bytes:
    """
    The whole viewport starting at line `top` (one based), as bytes.

    `rows` counts the footer: the last row holds the position and the
    rest hold document lines. Every line is exactly `columns` wide, so
    no erase-to-end-of-line is needed and a step's bytes are decided
    by the width alone.
    """
    out: list[str] = ["\x1b[H"]
    for row in range(rows - 1):
        number = top + row
        if number > LINES:
            out.append("~".ljust(columns))
        else:
            gutter = "%6d  " % number
            text = document_line(number)[: max(0, columns - len(gutter))]
            line = (gutter + text).ljust(columns)[:columns]
            if styled:
                line = "\x1b[1;36m%s\x1b[0m%s" % (gutter.rstrip(), line[len(gutter.rstrip()) :])
                if number % 7 == 0:
                    line = "\x1b[7m" + line + "\x1b[0m"
            out.append(line)
    footer = "-- scroll %d/%d (%s) --" % (top, LINES, "styled" if styled else "plain")
    out.append("\x1b[1m" + footer.ljust(columns)[:columns] + "\x1b[0m")
    return "".join(out).encode()


def scroll_step(top: int, direction: int, rows: int, columns: int, *, mode: str, styled: bool) -> bytes:
    """
    The bytes moving from `top` one line in `direction` (+1 down, -1
    up) emits, clamped to the document.

    In `redraw` mode that is the whole viewport again. In `scroll`
    mode it is one inserted or deleted line inside the scroll region
    plus the line that came into view, and the footing the region
    needs: set it around the viewport, move inside it, then release
    it to draw the footer outside.
    """
    height = rows - 1
    top = min(max(1, top + direction), LINES - height + 1)
    if mode == "redraw":
        return viewport_bytes(top, rows, columns, styled=styled)
    if direction > 0:
        line = _viewport_line(top + height - 1, columns, styled=styled)
        return (
            "".join(
                (
                    "\x1b[1;%dr" % height,
                    "\x1b[%d;1H\n" % height,
                    line,
                    "\x1b[r",
                    _footer(top, rows, columns, styled=styled),
                )
            )
        ).encode()
    line = _viewport_line(top, columns, styled=styled)
    # Reverse index, `ESC M` with no bracket: `CSI M` would delete a
    # line instead, which is the other direction entirely.
    return (
        "".join(
            (
                "\x1b[1;%dr" % height,
                "\x1b[1;1H\x1bM",
                line,
                "\x1b[r",
                _footer(top, rows, columns, styled=styled),
            )
        )
    ).encode()


def _viewport_line(number: int, columns: int, *, styled: bool) -> str:
    "One viewport line, exactly `columns` wide."
    if number > LINES:
        return "~".ljust(columns)
    gutter = "%6d  " % number
    text = document_line(number)[: max(0, columns - len(gutter))]
    line = (gutter + text).ljust(columns)[:columns]
    if styled:
        line = "\x1b[1;36m%s\x1b[0m%s" % (gutter.rstrip(), line[len(gutter.rstrip()) :])
        if number % 7 == 0:
            line = "\x1b[7m" + line + "\x1b[0m"
    return line


def _footer(top: int, rows: int, columns: int, *, styled: bool) -> str:
    "The footing `scroll_step` redraws after releasing the region."
    footer = "-- scroll %d/%d (%s) --" % (top, LINES, "styled" if styled else "plain")
    return "\x1b[%d;1H\x1b[1m%s\x1b[0m" % (rows, footer.ljust(columns)[:columns])


def window_size() -> tuple[int, int]:
    "The terminal's rows and columns, from the kernel."
    rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    return max(3, rows), max(20, columns)


class Viewer:
    "The state a hand moves: where the viewport is and how it draws."

    def __init__(self, *, mode: str, styled: bool):
        self.mode = mode
        self.styled = styled
        self.top = 1
        self.rows, self.columns = window_size()

    @property
    def height(self) -> int:
        return self.rows - 1

    def draw(self) -> None:
        sys.stdout.write(viewport_bytes(self.top, self.rows, self.columns, styled=self.styled).decode())
        sys.stdout.flush()

    def move(self, direction: int, pages: bool = False) -> bool:
        """
        Move one step, redrawing what the move needs. Past either end
        the viewport stays, and what is written depends on the program
        this viewer stands in for: `scroll` sits silent like `less`,
        and `redraw` paints the same viewport again, the way a program
        that redraws on every key does when the key moves nothing.
        The answer is whether anything was written at all.
        """
        step = self.height if pages else 1
        coming = self.top + direction * step
        clamped = min(max(1, coming), LINES - self.height + 1)
        if clamped == self.top:
            if self.mode == "redraw":
                self.draw()
                return True
            return False
        self.top = clamped
        if self.mode == "redraw" or pages:
            # Pages redraw whole even in scroll mode: that is what
            # `less` does, and one region step per line would be a
            # screenful of inserts anyway.
            self.draw()
        else:
            sys.stdout.write(
                scroll_step(
                    self.top - direction,
                    direction,
                    self.rows,
                    self.columns,
                    mode="scroll",
                    styled=self.styled,
                ).decode()
            )
            sys.stdout.flush()
        return True


def read_key() -> str:
    "One key, with arrows and SGR wheel events decoded to names."
    first = sys.stdin.read(1)
    if first != "\x1b":
        return (
            {"q": "quit", "j": "down", "k": "up", " ": "pagedown", "b": "pageup", "g": "home", "G": "end"}[first]
            if first in "qjk b gG"
            else "other"
        )
    second = sys.stdin.read(1)
    if second != "[":
        return "other"
    third = sys.stdin.read(1)
    if third == "<":
        event = ""
        while True:
            byte = sys.stdin.read(1)
            if byte in "mM" or byte == "":
                break
            event += byte
        parts = event.split(";")
        if len(parts) == 3 and parts[0] in ("64", "65"):
            return "down" if parts[0] == "64" else "up"
        return "other"
    return {"A": "up", "B": "down", "H": "home", "F": "end"}.get(third, "other")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scroll a long document on the alternate screen.")
    parser.add_argument("--mode", choices=("redraw", "scroll"), default="redraw")
    parser.add_argument("--styled", action="store_true")
    args = parser.parse_args(argv)

    viewer = Viewer(mode=args.mode, styled=args.styled)
    resized = False

    def on_resize(_signum, _frame) -> None:
        nonlocal resized
        resized = True

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?1000h\x1b[?1006h")
    try:
        tty.setraw(fd)
        signal.signal(signal.SIGWINCH, on_resize)
        viewer.draw()
        while True:
            if resized:
                resized = False
                viewer.rows, viewer.columns = window_size()
                viewer.top = min(viewer.top, LINES - viewer.height + 1)
                viewer.draw()
                continue
            key = read_key()
            if key == "quit":
                return 0
            if key == "down":
                viewer.move(+1)
            elif key == "up":
                viewer.move(-1)
            elif key == "pagedown":
                viewer.move(+1, pages=True)
            elif key == "pageup":
                viewer.move(-1, pages=True)
            elif key == "home":
                viewer.top = 1
                viewer.draw()
            elif key == "end":
                viewer.top = LINES - viewer.height + 1
                viewer.draw()
    finally:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
