"""
What the shell's Tab gets from `pymux`, through argcomplete: the same
options `arguments.py` reads, with the values a server can name (its
sessions, windows and panes) asked of the running server, and the paths
a person may mean.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
from functools import partial
from typing import TYPE_CHECKING

from pymux.client import is_ssh_url
from pymux.entry_points.arguments import Mode, add_options

if TYPE_CHECKING:
    pass

__all__ = ["ASK_SECONDS"]


def _completion_parser() -> argparse.ArgumentParser:
    """
    The command line of pymux, as a parser that can complete it.

    The real parse of the entry point collects everything after the
    options and hands it to a server as one string, so it cannot say
    what a command takes. This one holds every command of the server
    under it, with the options each takes, and argcomplete answers
    through it. It parses nothing on its own.
    """
    from pymux.commands import add_commands_to

    # Help and version are options of the real parse, so a
    # completion offers them; nothing is excluded. The commands
    # under this declare their own flags, and `-h` of `split-window`
    # is one of them.
    parser = argparse.ArgumentParser(
        prog="pymux",
        description="pymux: terminal multiplexer.",
        allow_abbrev=False,
    )
    add_options(parser, suppress_defaults=False)
    modes = parser.add_subparsers(metavar="COMMAND")
    # `list-sessions` and `ls` come from the command tree below, with
    # the options the command takes; what is left here is the modes
    # the tree does not name: the starts, `find`, and `web`.
    for mode in (
        Mode.INTEGRATED,
        Mode.START_SERVER,
        Mode.ATTACH,
        Mode.FIND,
        Mode.DIAGNOSE,
        Mode.RENICE,
        Mode.WEB,
        Mode.SEND_FILE,
        Mode.IMGCAT,
    ):
        mode_parser = modes.add_parser(
            str(mode),
            help="Run a server, or attach to one. See `pymux --help`.",
        )
        add_options(mode_parser, suppress_defaults=True)
        if mode is Mode.DIAGNOSE:
            mode_parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        if mode is Mode.RENICE:
            mode_parser.add_argument("niceness", nargs="?", help="From -20 to 19; -10 by default.")
        if mode is Mode.SEND_FILE:
            mode_parser.add_argument("files", nargs="+", help="The files to send to the person.")
        if mode is Mode.IMGCAT:
            mode_parser.add_argument("-w", "--width", type=int, help="The columns an image covers.")
            mode_parser.add_argument("files", nargs="+", help="The images to show; - reads standard input.")
    add_commands_to(modes)
    from pymux.commands.completer import completes_from_the_server

    for name, command_parser in modes.choices.items():
        for action in command_parser._actions:
            if completes_from_the_server(name, action):
                action.completer = partial(_server_values, name, action.dest)  # pyrefly: ignore[missing-attribute]
    for action in modes.choices["receive"]._actions:
        if action.dest == "paths":
            action.completer = _person_paths  # pyrefly: ignore[missing-attribute]
    return parser


#: How long a Tab waits for a server that took the connection. A server
#: answers a value in a quarter of a second (`QUESTION_BUDGET`); this
#: is for a peer that never answers.
ASK_SECONDS = 1.0


def _ask_server(words: list[str], socket_name: str | None = None, timeout: float = ASK_SECONDS) -> list[str]:
    """
    The lines a command prints, run on the server `-S` names or the
    one of this pane. Nothing when there is none, when it is another
    machine's (a Tab does not wait for SSH), or when the command fails.
    """
    import io

    from pymux.client.command import CommandClient

    pane_socket, _, pane_id = os.environ.get("PYMUX", "").rpartition(",")
    socket_name = socket_name or pane_socket
    if not socket_name or is_ssh_url(socket_name):
        return []
    printed = io.StringIO()
    try:
        client = CommandClient(socket_name)
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            code = client.run_command(shlex.join(words), pane_id or None, timeout=timeout)
    except OSError:
        return []
    if code != 0:
        return []
    return printed.getvalue().splitlines()


def _server_values(name: str, dest: str, prefix: str, parsed_args: argparse.Namespace, **_) -> dict[str, str]:
    """
    The values the server offers for this argument, each with what it
    says: the command bar's own completion, asked over the socket.
    """
    given = {
        key: value
        for key, value in vars(parsed_args).items()
        if not key.startswith("_") and key != "socket" and isinstance(value, (str, bool, int, list))
    }
    lines = _ask_server(
        ["complete-value", "--", name, dest, prefix, json.dumps(given)],
        getattr(parsed_args, "socket", None),
    )
    return dict(line.partition("\t")[::2] for line in lines if line)


def _person_paths(prefix: str, **_) -> list[str]:
    """
    The paths on the person's machine that start with `prefix`, asked
    of the server of this pane: `list-person-files` on the directory
    the prefix names. Nothing, outside a pane or when the client says
    nothing. Lillecarl/pymux#748.
    """
    from pymux.connection.uploads import LIST_SECONDS

    if not os.environ.get("PYMUX"):
        return []
    head = prefix[: prefix.rfind("/") + 1]
    lines = _ask_server(["list-person-files", head or "~"], timeout=LIST_SECONDS + 1)
    return [head + name for name in lines if name]


def answer_completion() -> None:
    """
    Answer a shell completion, when this start is one.

    argcomplete is imported here and not at the top of the module: it
    is dozens of modules, and the shell reaches this only on a Tab.
    """
    import argcomplete

    from pymux.commands.completer import stop_shlex_comments

    stop_shlex_comments()
    argcomplete.autocomplete(_completion_parser())
