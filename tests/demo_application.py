"""
The program in the pane, for the pictures of every theme.

It paints the screen with what a program paints with: the sixteen
colours it asked its pane for, bold, italic, underline, reverse, and
the shapes a busy pane carries - a file listing, a diff, a progress
bar, a table - so that a theme is read around content rather than
around an empty shell. The theme itself draws around all of it: the
status line, the title bars, the borders, the pop-ups.

It asks for its colours the way a program does ("OSC 4", "OSC 10",
"OSC 11") and paints with what it is told, in 24 bit colour. It
paints at once, in the conventional colours, and redraws when the
replies arrive -- so the screen is never blank and never waits, and
the quiet that follows the redraw is what a picture waits for. What
the pane says is what the pane draws: a picture of a themed session
shows the theme's palette inside the pane, and a picture of a bare
terminal shows that terminal's.

Every row fits half a screen, because the picture splits the window
and runs this in one of the halves. It hides the cursor while it
parks: a screen with a blinking cursor never settles, and the picture
is taken by waiting for two identical frames.

It parks until it is killed, so a photograph taken later still finds
it on the screen.
"""

import re
import shutil
import sys
import termios

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
REVERSE = "\x1b[7m"
RESET = "\x1b[0m"

#: The conventional sixteen, as hex. A pane that answers nothing
#: leaves this program painting the colours every terminal paints,
#: which is what it did before it learned to ask.
CONVENTIONAL = [
    "#000000", "#cd0000", "#00cd00", "#cdcd00",
    "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00",
    "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
]

#: The names, for the row under the swatches.
ANSI_NAMES = [
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright black",
    "bright red",
    "bright green",
    "bright yellow",
    "bright blue",
    "bright magenta",
    "bright cyan",
    "bright white",
]

#: The asks: the sixteen by number, then the two defaults.
ASKS = "".join("\x1b]4;%d;?\x1b\\" % index for index in range(16)) + (
    "\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
)


def _rgb(spec):
    "A colour spec as (r, g, b), or None for one this does not read."
    spec = spec.strip()
    if spec.startswith("#") and len(spec) == 7:
        return tuple(int(spec[i : i + 2], 16) for i in (1, 3, 5))
    if spec.startswith("rgb:"):
        parts = spec[4:].split("/")
        if len(parts) == 3:
            return tuple(int(part[:2], 16) for part in parts)
    return None


def parse_replies(data: bytes) -> dict:
    """
    The colours in OSC replies, as a dict by name.

    Reads "OSC 4;index;spec", "OSC 10;spec" and "OSC 11;spec", ended
    by BEL or ST, the two shapes a pane answers in. A colour is keyed
    by its number, and the two defaults by their names. Everything
    else in the bytes -- key presses, another program's output -- is
    ignored, because a pane's stdin carries whatever it carries.
    """
    colours = {}
    for match in re.finditer(rb"\x1b\]([0-9]+);([^\x07\x1b]*)(?:\x07|\x1b\\)", data):
        code = match.group(1).decode("ascii")
        payload = match.group(2).decode("ascii", "replace")
        if code == "4":
            index, _, spec = payload.partition(";")
            if index.isdigit() and int(index) < 16:
                rgb = _rgb(spec)
                if rgb is not None:
                    colours[int(index)] = rgb
        elif code in ("10", "11"):
            rgb = _rgb(payload)
            if rgb is not None:
                colours["foreground" if code == "10" else "background"] = rgb
    return colours




class Painter:
    """
    The SGR sequences that draw in the colours the pane said.

    Every colour is 24 bit: the pane's answers describe what this
    pane draws, and a number would ask the terminal of the user to
    pick from its own palette, which is a different scheme.
    """

    def __init__(self, colours):
        self.colours = colours

    def rgb(self, number):
        "The colour of one palette entry, from the answer or the convention."
        rgb = self.colours.get(number)
        if rgb is None:
            rgb = _rgb(CONVENTIONAL[number])
        return rgb

    def fg(self, number):
        return "\x1b[38;2;%d;%d;%dm" % self.rgb(number)

    def bg(self, number):
        return "\x1b[48;2;%d;%d;%dm" % self.rgb(number)


def create_line(*parts):
    "One row of the screen from styled pieces."
    return "".join(parts) + RESET


def colours(p):
    """
    The sixteen, as blocks in the colour each names, two rows of
    eight, each block two cells wide with its name under it.
    """
    rows = []
    for half in (0, 8):
        blocks = ""
        names = ""
        for number, name in zip(range(half, half + 8), ANSI_NAMES[half : half + 8]):
            blocks += "%s   %s" % (p.bg(number), RESET)
            names += " %-2s " % (name[:2].capitalize(),)
        rows.append(create_line("  ", blocks))
        rows.append(create_line("  ", DIM, names, RESET))
    return rows


def files(p):
    "A listing, as one paints it: the kinds of a file in their colours."
    return [
        create_line("  ", BOLD, "drwxr-xr-x", RESET, "  ", p.fg(4), BOLD, "src", RESET),
        create_line("  ", BOLD, "-rw-r--r--", RESET, "  ", p.fg(2), "README.md", RESET),
        create_line("  ", BOLD, "-rwxr-xr-x", RESET, "  ", p.fg(11), "build.sh", RESET),
        create_line("  ", BOLD, "-rw-r--r--", RESET, "  ", p.fg(8), ".gitignore", RESET),
        create_line(
            "  ",
            BOLD,
            "lrwxrwxrwx",
            RESET,
            "  ",
            p.fg(6),
            "latest",
            RESET,
            " -> ",
            "%s%s" % (p.fg(6), UNDERLINE),
            "2026-09-11",
            RESET,
        ),
    ]


def diff(p):
    "A diff: what a pane that carries one shows for it."
    return [
        create_line("  ", p.fg(8), "@@ -12,4 +12,5 @@", RESET),
        create_line("  ", p.fg(1), "-", "the colour of the pane, chosen once", RESET),
        create_line(
            "  ", p.fg(2), "+", "a role named once, and the rules it makes", RESET
        ),
        create_line(
            "  ", p.fg(2), "+", "a theme from the styles of pygments", RESET
        ),
        create_line("  ", " ", "  the border keeps its own colour", RESET),
    ]


def progress(p):
    "A bar of equals, filled as far as it says."
    done, total, width = 17, 24, 30
    filled = int(width * done / total)
    bar = "=" * filled + "-" * (width - filled)
    return create_line("  ", p.fg(5), "[", bar, "]", RESET, " %d of %d" % (done, total))


def processes():
    "A table in plain characters, headed in bold."
    return [
        create_line("  ", BOLD, "PID     TIME    WHAT", RESET),
        create_line("  ", DIM, "1       0:00    the init of it all", RESET),
        create_line("  ", DIM, "42      3:14    the theme, reading the screen", RESET),
        create_line("  ", DIM, "99      0:07    the pane, holding it", RESET),
    ]


def screen_rows(p):
    "Everything the screen says, in order."
    return (
        [
            create_line("", REVERSE, BOLD, " THE PANE, HOLDING A PROGRAM ", RESET),
            "",
            "  A theme draws around this: the status line,",
            "  the title bars, the borders. The colours in",
            "  the pane are what it asked for, and got.",
            "",
        ]
        + files(p)
        + [""]
        + diff(p)
        + [""]
        + [progress(p)]
        + [""]
        + colours(p)
        + [""]
        + processes()
    )


def draw(p, rows):
    "The whole screen, from the top, in the colours the painter holds."
    sys.stdout.write("\x1b[2J\x1b[H")
    for line in screen_rows(p)[: rows - 1]:
        sys.stdout.write(line + "\r\n")
    tail = " demo_application.py  ·  one program, every theme "
    sys.stdout.write("\x1b[%d;1H%s" % (rows, create_line(DIM, " ready ", RESET) + tail))
    sys.stdout.flush()


def main():
    columns, rows = shutil.get_terminal_size()

    # Hidden while it parks, and left hidden: a blinking cursor is the
    # one thing that keeps the screen from settling, and the pane dies
    # with the program, so nobody inherits the state.
    sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H")

    # The echo of the pane's tty is off for good. What the pane writes
    # back is input to this program, and a tty in the ordinary mode
    # echoes its input onto the screen: the replies would land beside
    # the picture as text, which is what the first capture showed.
    # Every program that reads replies runs with the echo off, and
    # this one does the same.
    attributes = termios.tcgetattr(sys.stdin)
    attributes[3] &= ~termios.ECHO
    termios.tcsetattr(sys.stdin, termios.TCSANOW, attributes)

    sys.stdout.write(ASKS)
    sys.stdout.flush()

    # The first paint is the conventional sixteen, and the screen is
    # never blank while the replies travel. The park is the read: a
    # redraw when what the pane said differs from what it said, and
    # quiet after, which is what a photograph waits for.
    p = Painter({})
    draw(p, rows)

    collected = b""
    while True:
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            break  # The pane is gone from behind this program.
        collected += chunk
        colours = parse_replies(collected)
        if colours != p.colours:
            p = Painter(colours)
            draw(p, rows)


if __name__ == "__main__":
    main()
