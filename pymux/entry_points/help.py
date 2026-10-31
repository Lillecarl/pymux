"""
`pymux <command> --help`: the help of one command or mode word, read on
this side with no server. The command table is in the client's own
build, so the help is the parser that runs the command. Lillecarl/pymux#845.
"""

from __future__ import annotations

import argparse

from .arguments import Mode, add_options

__all__ = ["asks_for_help", "help_of"]

#: The words that ask for help after a command.
HELP_FLAGS = ("--help", "-h")

#: What each mode word does, for its help. The mode words the server
#: also takes as a command, `ls` and `list-sessions`, show the
#: command's help instead: their flags go to the server.
MODE_HELP: dict[Mode, str] = {
    Mode.INTEGRATED: (
        "Start a server and one client in this process, on this terminal. "
        "The words after it are the command of the first pane."
    ),
    Mode.START_SERVER: "Start a server and leave it running, with no client.",
    Mode.ATTACH: "Attach this terminal to the newest server, or to the one -S names.",
    Mode.FIND: (
        "Print the socket a local `pymux attach` would take, newest first. "
        "An ssh:// client runs this on the other machine."
    ),
    Mode.DIAGNOSE: "Print what this machine sees, for a support conversation.",
    Mode.RENICE: "Set the niceness of every server of this user, or of the one -S names.",
    Mode.WEB: "Serve a pane in a browser, with a token as the guard.",
    Mode.SEND_FILE: "Send files to the person, from a pane, over the terminal.",
    Mode.IMGCAT: "Show images in this terminal.",
    Mode.CHECK_SNAPSHOT: "Check that this build reads an upgrade's snapshot. An upgrading server runs it.",
    Mode.RESUME_SERVER: "Resume a server from an upgrade's snapshot. An upgrading server runs it.",
}

#: The modes whose words are the server's command.
_THE_SERVERS = (Mode.LIST_SESSIONS, Mode.LS)


def _command_parser(word: str) -> argparse.ArgumentParser | None:
    "The parser of the command this word names, alias or not, or None."
    from pymux.commands import build_parser_tree
    from pymux.commands.aliases import ALIASES

    _parser, subparsers = build_parser_tree()
    return subparsers.choices.get(ALIASES.get(word, word))


def _mode_parser(mode: Mode) -> argparse.ArgumentParser:
    "A parser that says what a mode word takes."
    parser = argparse.ArgumentParser(
        prog="pymux %s" % (mode,),
        description=MODE_HELP[mode],
        add_help=False,
    )
    add_options(parser, suppress_defaults=False)
    match mode:
        case Mode.INTEGRATED:
            parser.add_argument("command", nargs=argparse.REMAINDER, help="What the first pane runs.")
        case Mode.DIAGNOSE:
            parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        case Mode.RENICE:
            parser.add_argument("niceness", nargs="?", help="From -20 to 19.")
        case Mode.SEND_FILE:
            parser.add_argument("paths", nargs="+", metavar="PATH", help="The files to send.")
        case Mode.CHECK_SNAPSHOT | Mode.RESUME_SERVER:
            parser.add_argument("path", metavar="PATH", help="The snapshot.")
    return parser


def _parser_of(word: str) -> argparse.ArgumentParser | None:
    try:
        mode = Mode(word)
    except ValueError:
        return _command_parser(word)
    if mode in _THE_SERVERS:
        return _command_parser(word)
    return _mode_parser(mode)


def asks_for_help(words: list[str]) -> bool:
    """
    Whether these words, a command or mode word first, ask for its help.

    A help flag counts before a `--`, and nowhere after it. `-h` counts
    only for a command that has no `-h` of its own: `split-window -h`
    splits side by side. After `integrated` only the next word counts,
    because the rest is a program's command line. `imgcat` reads its
    own words, help included.
    """
    if not words or words[0] == Mode.IMGCAT:
        return False
    if words[0] == Mode.INTEGRATED:
        return words[1:2] in (["--help"], ["-h"])
    options = words[1 : words.index("--")] if "--" in words else words[1:]
    if "--help" in options:
        return True
    if "-h" not in options:
        return False
    parser = _parser_of(words[0])
    return parser is not None and "-h" not in parser._option_string_actions


def help_of(word: str) -> str | None:
    "The help of the command or mode this word names, or None for a word that names neither."
    parser = _parser_of(word)
    if parser is None:
        return None
    return parser.format_help()
