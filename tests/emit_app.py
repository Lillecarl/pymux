"""
A line emitter for scroll benchmarks, and the second app of the collection.

Run it in any terminal and it prints numbered lines as fast as the
terminal takes them, then says how long that took. `less` stands in
for the programs that scroll a viewport; this one stands in for the
programs that scroll by writing: `cat`, a compiler, a log tail.

Two shapes of line, because the terminal does different work for
them:

- **wide**: each line runs two and a half screens across and the
  terminal wraps it. That is a program that never heard of the width,
  and the wrap is the terminal's.
- **broken**: each line is broken at the width by the program. That is
  `ls`, a test runner, anything that lays its own output out.

`--alt` prints on the alternate screen instead, where there is no
scrollback and a line past the bottom scrolls the screen. The
alternate screen wraps wide lines the way the main one does --
`pyte` was asked -- so all four combinations run.

`--styled` colours the gutter and marks every seventh line, the way
highlighted output does. Styles ride the wrap in `wide` mode, which
is where a parser earns its keep.

The benchmark does not run this file. It imports `emit_chunk` and
feeds what it returns into a pane, so the bytes a profile attributes
are byte for byte what the emitter emits. One source of truth, two
users. The lines are numbered from `start`, so successive chunks are
an endless scroll and a test can read how far the viewport advanced.

The text comes from `scroll_app.document_line`, the collection's one
source of prose. `python-lorem` would write prettier lines, but the
benchmark would then need the package in the sandbox for no
difference that matters here: what the shapes cost is in the breaks
and the wraps, not in the words.
"""

from __future__ import annotations

import argparse
import fcntl
import struct
import sys
import termios
import time

from scroll_app import LINES, document_line

#: How many screen widths a `wide` line spans. Past two the terminal
#: wraps twice, which is the whole of the wrapping there is to pay.
WIDE_SPANS = 2.5


def emit_line(number: int, columns: int, *, mode: str, styled: bool) -> str:
    """
    Line `number` (one based) as the emitter writes it, without its
    line break.
    """
    gutter = "%6d  " % number
    if mode == "wide":
        width = int(columns * WIDE_SPANS)
        text = (" ".join(document_line(number + shift) for shift in range(9)) + " ")[: max(0, width - len(gutter))]
        line = gutter + text
        if styled:
            line = "\x1b[1;36m%s\x1b[0m%s" % (gutter.rstrip(), line[len(gutter.rstrip()) :])
            if number % 7 == 0:
                line = "\x1b[7m" + line + "\x1b[0m"
        return line
    text = document_line(number)[: max(0, columns - len(gutter))]
    # Natural length: a program that breaks its own lines writes no
    # padding, and the line break positions the next one.
    line = gutter + text
    if styled:
        line = "\x1b[1;36m%s\x1b[0m%s" % (gutter.rstrip(), line[len(gutter.rstrip()) :])
        if number % 7 == 0:
            line = "\x1b[7m" + line + "\x1b[0m"
    return line


def emit_chunk(start: int, count: int, columns: int, *, mode: str, styled: bool) -> bytes:
    """
    `count` lines from `start` (one based), each with its line break.

    A `wide` line ends once: the terminal breaks it. A `broken` line
    is already the width, padded so no erase is needed.
    """
    return "".join(
        emit_line(start + number, columns, mode=mode, styled=styled) + "\r\n" for number in range(count)
    ).encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print numbered lines as fast as the terminal takes them.")
    parser.add_argument("--mode", choices=("wide", "broken"), default="broken")
    parser.add_argument("--alt", action="store_true")
    parser.add_argument("--styled", action="store_true")
    parser.add_argument("--lines", type=int, default=LINES)
    parser.add_argument("--columns", type=int, default=0)
    args = parser.parse_args(argv)

    columns = args.columns
    if not columns:
        _, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        columns = max(20, columns)

    out = sys.stdout
    if args.alt:
        out.write("\x1b[?1049h\x1b[H")
    started = time.perf_counter()
    step = 0
    while step < args.lines:
        count = min(89, args.lines - step)
        out.write(emit_chunk(1 + step, count, columns, mode=args.mode, styled=args.styled).decode())
        out.flush()
        step += count
    took = time.perf_counter() - started
    report = "%d lines in %.3fs\r\n" % (args.lines, took)
    if args.alt:
        out.write("\x1b[?1049l")
    out.write(report)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
