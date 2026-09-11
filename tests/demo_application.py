"""
The program in the pane, for the pictures of every theme.

It paints the screen with what a program paints with: the sixteen ANSI
colours, bold, italic, underline, reverse, and the shapes a busy pane
carries - a file listing, a diff, a progress bar, a table - so that a
theme is read around content rather than around an empty shell. The
theme itself draws around all of it: the status line, the title bars,
the borders, the pop-ups.

Every row fits half a screen, because the picture splits the window
and runs this in one of the halves. It hides the cursor while it
parks: a screen with a blinking cursor never settles, and the picture
is taken by waiting for two identical frames.

It parks until it is killed, so a photograph taken later still finds
it on the screen.
"""

import shutil
import sys
import time

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
REVERSE = "\x1b[7m"
RESET = "\x1b[0m"

#: The sixteen, as SGR numbers: eight dim, eight bright.
ANSI = [30, 31, 32, 33, 34, 35, 36, 37, 90, 91, 92, 93, 94, 95, 96, 97]
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


def create_line(*parts):
    "One row of the screen from styled pieces."
    return "".join(parts) + RESET


def colours():
    """
    The sixteen, as blocks on the background they name, two rows of
    eight, each block two cells wide with its name under it.
    """
    rows = []
    for half in (0, 8):
        blocks = ""
        names = ""
        for code, name in zip(ANSI[half : half + 8], ANSI_NAMES[half : half + 8]):
            blocks += "\x1b[7m\x1b[%dm   \x1b[0m" % (code,)
            names += " %-2s " % (name[:2].capitalize(),)
        rows.append(create_line("  ", blocks))
        rows.append(create_line("  ", DIM, names, RESET))
    return rows


def files():
    "A listing, as one paints it: the kinds of a file in their colours."
    return [
        create_line("  ", BOLD, "drwxr-xr-x", RESET, "  ", "\x1b[34m\x1b[1m", "src", RESET),
        create_line("  ", BOLD, "-rw-r--r--", RESET, "  ", "\x1b[32m", "README.md", RESET),
        create_line("  ", BOLD, "-rwxr-xr-x", RESET, "  ", "\x1b[93m", "build.sh", RESET),
        create_line("  ", BOLD, "-rw-r--r--", RESET, "  ", "\x1b[90m", ".gitignore", RESET),
        create_line(
            "  ",
            BOLD,
            "lrwxrwxrwx",
            RESET,
            "  ",
            "\x1b[36m",
            "latest",
            RESET,
            " -> ",
            "\x1b[36m\x1b[4m",
            "2026-09-11",
            RESET,
        ),
    ]


def diff():
    "A diff: what a pane that carries one shows for it."
    return [
        create_line("  ", "\x1b[90m", "@@ -12,4 +12,5 @@", RESET),
        create_line("  ", "\x1b[31m", "-", "the colour of the pane, chosen once", RESET),
        create_line(
            "  ", "\x1b[32m", "+", "a role named once, and the rules it makes", RESET
        ),
        create_line(
            "  ", "\x1b[32m", "+", "a theme from the styles of pygments", RESET
        ),
        create_line("  ", " ", "  the border keeps its own colour", RESET),
    ]


def progress():
    "A bar of equals, filled as far as it says."
    done, total, width = 17, 24, 30
    filled = int(width * done / total)
    bar = "=" * filled + "-" * (width - filled)
    return create_line("  ", "\x1b[35m", "[", bar, "]", RESET, " %d of %d" % (done, total))


def processes():
    "A table in plain characters, headed in bold."
    return [
        create_line("  ", BOLD, "PID     TIME    WHAT", RESET),
        create_line("  ", DIM, "1       0:00    the init of it all", RESET),
        create_line("  ", DIM, "42      3:14    the theme, reading the screen", RESET),
        create_line("  ", DIM, "99      0:07    the pane, holding it", RESET),
    ]


def screen_rows():
    "Everything the screen says, in order."
    return (
        [
            create_line("", REVERSE, BOLD, " THE PANE, HOLDING A PROGRAM ", RESET),
            "",
            "  A theme draws around this: the status line,",
            "  the title bars, the borders. The colours in",
            "  the pane are the program's own, and stay.",
            "",
        ]
        + files()
        + [""]
        + diff()
        + [""]
        + [progress()]
        + [""]
        + colours()
        + [""]
        + processes()
    )


def main():
    columns, rows = shutil.get_terminal_size()

    # Hidden while it parks, and left hidden: a blinking cursor is the
    # one thing that keeps the screen from settling, and the pane dies
    # with the program, so nobody inherits the state.
    sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H")
    for line in screen_rows()[: rows - 1]:
        sys.stdout.write(line + "\r\n")

    tail = " demo_application.py  ·  one program, every theme "
    sys.stdout.write("\x1b[%d;1H%s" % (rows, create_line(DIM, " ready ", RESET) + tail))
    sys.stdout.flush()

    # Parked, so the photograph finds it where it was left.
    time.sleep(300)


if __name__ == "__main__":
    main()
