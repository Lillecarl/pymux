"""
The command line of `pymux`: the modes it starts in, every option, and
the parser that reads them. `run_pymux.run` acts on what this reads, and
`completion.py` builds its own parser from the same options.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from enum import StrEnum
from typing import TYPE_CHECKING

from pymux import __version__, log

if TYPE_CHECKING:
    pass

__all__ = ["Mode", "parse_arguments"]


class Mode(StrEnum):
    """
    The word that chooses what this invocation of pymux starts as.

    A word that is not one of these is a pymux command for a running
    server. `parse_arguments` answers a member or `None`, and `run`
    matches on it, so a mode added and not handled is an error and not a
    silent fall through.
    """

    INTEGRATED = "integrated"
    START_SERVER = "start-server"
    ATTACH = "attach"
    LIST_SESSIONS = "list-sessions"
    LS = "ls"
    FIND = "find"
    DIAGNOSE = "diagnose"
    RENICE = "renice"
    WEB = "web"
    #: Send files to the person, from a pane. Not `send`, which is
    #: tmux's name for `send-keys`. Lillecarl/pymux#743.
    SEND_FILE = "send-file"
    #: Show images in the terminal it runs in. Lillecarl/pymux#764.
    IMGCAT = "imgcat"
    #: An upgrade's check of the new build, and its new server.
    #: `pymux/core/upgrade.py`.
    CHECK_SNAPSHOT = "check-snapshot"
    RESUME_SERVER = "resume-server"


def _mode_of(word: str) -> Mode | None:
    "The mode this word names, or `None` when the word is a command."
    try:
        return Mode(word)
    except ValueError:
        return None


def how_much_to_log(chosen: str | None) -> int:
    """
    What `--log-level` asked for, or INFO. `pymux/log.py` says why INFO.

    The names are `log.LogLevel`, which is also what `set-option
    log-level` takes, so the flag and the option say the same words and
    reach the same place.
    """
    if not chosen:
        return logging.INFO
    return log.LEVELS[log.LogLevel(chosen)]


def add_options(parser: argparse.ArgumentParser, suppress_defaults: bool) -> None:
    """
    Add the pymux options to the parser.

    :param suppress_defaults: When `True`, don't set defaults. (Used for the
        second parse pass, so that options given after the mode word don't
        clobber the ones given before it.)
    """
    default = argparse.SUPPRESS if suppress_defaults else None
    false = argparse.SUPPRESS if suppress_defaults else False

    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument(
        "-V",
        dest="show_tmux_version",
        action="store_true",
        help="Print the version of the tmux interface that pymux emulates.",
    )
    parser.add_argument(
        "-S",
        "--socket",
        dest="socket",
        metavar="SOCKET",
        default=default,
        help="Unix socket path. A number is accepted as well, and so is "
        "ssh://[user@]host[:port]/path, which reaches a server on that "
        "machine and draws its panes here.",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="filename",
        metavar="FILE",
        default=default,
        help="Path to configuration file. Without it, the first of "
        "'$XDG_CONFIG_HOME/pymux/pymux.conf' and '~/.pymux.conf' that is there.",
    )
    parser.add_argument(
        "-d",
        "--detach-others",
        dest="detach_others",
        action="store_true",
        default=false,
        help="Detach all other clients, when attaching.",
    )
    parser.add_argument(
        "-x",
        "--hang-up-others",
        dest="hang_up_others",
        action="store_true",
        default=false,
        help=(
            "Detach the other clients of that session, and hang up the "
            "process each one was started by. The terminal each was in "
            "closes, rather than going back to a shell prompt."
        ),
    )
    parser.add_argument(
        "-r",
        "--read-only",
        dest="read_only",
        action="store_true",
        default=false,
        help=(
            "Only watch. This client types nothing into a pane and "
            "runs no command that changes anything, and it is left out "
            "of the size of the session, so a small terminal watching "
            "does not shrink it for the people working in it."
        ),
    )
    parser.add_argument(
        "-n",
        "--name",
        dest="client_name",
        metavar="NAME",
        default=default,
        help=(
            "What to call this terminal: 'desk', 'phone'. "
            "'detach-client -t' and 'set-client-option -t' take it, and "
            "so does the name pymux derives when this is not given. It "
            "lasts as long as the attachment, so put it in the command "
            "that attaches rather than expecting the server to remember."
        ),
    )
    parser.add_argument(
        "--port",
        dest="web_port",
        metavar="PORT",
        default=default,
        help="`pymux web`: the port to serve on. 8080 by default.",
    )
    parser.add_argument(
        "--bind",
        dest="web_bind",
        metavar="ADDRESS",
        default=default,
        help=(
            "`pymux web`: what to listen on. 127.0.0.1 by default, which "
            "is this machine only. Anything else lets another machine "
            "reach the terminal, with the token as the only guard and no "
            "TLS unless something else adds it."
        ),
    )
    parser.add_argument(
        "--allow-input",
        dest="web_allow_input",
        action="store_true",
        default=false,
        help=(
            "`pymux web`: let a viewer type into the pane. Off by "
            "default: a port that types into a terminal is a different "
            "thing from one that shows it."
        ),
    )
    parser.add_argument(
        "--log",
        dest="logfile",
        metavar="FILE",
        default=default,
        help="Logfile.",
    )
    parser.add_argument(
        "--log-level",
        dest="log_level",
        metavar="LEVEL",
        choices=sorted(log.LEVELS),
        default=default,
        help=(
            "How much to log. 'debug' adds a line for every frame the "
            "server draws, which is what a person debugging one wants "
            "and nobody else does. 'set-option log-level debug' says "
            "the same to a server that is already running."
        ),
    )
    parser.add_argument(
        "--truecolor",
        action="store_true",
        default=false,
        help="Render true color (24 bit) instead of 256 colors.",
    )
    parser.add_argument(
        "--ansicolor",
        action="store_true",
        default=false,
        help="Use only the 16 ANSI colors.",
    )


def _build_parser(with_positionals: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pymux",
        description="pymux: terminal multiplexer.",
        epilog="Any other arguments are sent to the running server as a pymux command. Example: pymux split-window",
    )
    add_options(parser, suppress_defaults=False)
    if with_positionals:
        # Collect the mode/command and all its arguments. Everything after
        # the first positional argument is taken verbatim.
        parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def _niceness(word: str) -> int:
    "A niceness, as `setpriority` takes it."
    value = int(word)
    if not -20 <= value <= 19:
        raise argparse.ArgumentTypeError("a niceness is from -20 to 19")
    return value


def parse_arguments(
    argv: list[str] | None = None,
) -> tuple[argparse.Namespace, Mode | None, str | None]:
    """
    Read the command line: the options, the mode and the command.

    Returns the options, the mode word (`None` when there is none) and
    the command as one string (`None` when there is none). A command is
    one string because that is what reaches a pane and what a server
    reads, and `shlex.quote` keeps an argument with a space in it in one
    piece on the way.

    `run` does the rest. This is separate so that a test can ask what a
    command line means without starting a server.
    """
    a = _build_parser().parse_args(argv)

    rest: list[str] = a.args
    command = None
    mode: Mode | None = _mode_of(rest[0]) if rest else None

    if mode is not None:
        # A mode word was given. The options after the mode word are parsed
        # as well. (argparse.REMAINDER above collected them verbatim.)
        mode_parser = argparse.ArgumentParser(prog="pymux", description="pymux: terminal multiplexer.")
        add_options(mode_parser, suppress_defaults=True)
        mode_args, extra = mode_parser.parse_known_args(rest[1:])
        for key, value in vars(mode_args).items():
            setattr(a, key, value)
        rest = extra

        # "--" says that everything after it is the command and not an
        # option of pymux. argparse consumes one while it assigns
        # positional arguments, and `mode_parser` declares none, so it
        # hands the separator back and it would become the first word of
        # the command. Only the first one: a second "--" is an argument
        # of the program that runs.
        if rest and rest[0] == "--":
            rest = rest[1:]

        if mode is Mode.DIAGNOSE:
            # Its own flags, before the fallthrough below would read
            # `--json` as a command for a server.
            flag_parser = argparse.ArgumentParser(add_help=False)
            flag_parser.add_argument("--json", action="store_true", default=False)
            flag_args, rest = flag_parser.parse_known_args(rest)
            a.diagnose_json = flag_args.json

        if mode is Mode.RENICE:
            # A positional, as `renice` takes it: `-n` is the client's name.
            level_parser = argparse.ArgumentParser(prog="pymux renice")
            level_parser.add_argument("niceness", nargs="?", type=_niceness)
            a.niceness = level_parser.parse_args(rest).niceness
            rest = []

        if mode is Mode.SEND_FILE:
            # The paths, before the fallthrough below sends them to a
            # server as a command.
            a.send_paths = rest
            rest = []

        if mode is Mode.IMGCAT:
            # Its files and flags, which `pymux.imgcat` reads.
            a.imgcat_words = rest
            rest = []

        if mode in (Mode.CHECK_SNAPSHOT, Mode.RESUME_SERVER):
            # What an upgrading server runs, never a person: the words
            # are `pymux.core.upgrade`'s to read.
            a.upgrade_words = rest
            rest = []

    if mode is Mode.INTEGRATED:
        # The command of the first pane, rather than a pymux command
        # for a running server.
        command = " ".join(shlex.quote(x) for x in rest) if rest else None
    elif mode is not None and rest:
        # A mode with extra arguments: e.g. `pymux list-sessions -F ...`.
        # The whole thing is sent to the server as one command.
        command = " ".join(shlex.quote(x) for x in (mode, *rest))
        mode = None
    elif rest:
        # Not a mode: all arguments form one pymux command.
        command = " ".join(shlex.quote(x) for x in rest)

    return a, mode, command
