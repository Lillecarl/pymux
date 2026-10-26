from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux
    from pymux.tags import Tags


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import session_of_target, the_pane, the_window
from pymux.jobs import JobId
from pymux.tags import parse_tag


def tags_of(pymux: Pymux, args: argparse.Namespace) -> Tags:
    """
    The tags of what `-s`, `-w`, `-p` or `-j` and `-t` name: a pane by
    default. Without `-t` it is the pane the caller means, or the window
    or session holding it; a job always needs its id.
    """
    if args.j:
        if not args.target or not args.target.isdigit():
            raise CommandException("-j needs a job id: -t <job>")
        job = pymux.jobs.get(JobId(int(args.target)))
        if job is None:
            raise CommandException("no job %s" % (args.target,))
        return job.tags
    if args.s:
        return session_of_target(pymux, args.target).tags
    if args.w:
        if args.target:
            return the_window(pymux, args.target).tags
        window = pymux._window_holding(the_pane(pymux, None))
        if window is None:
            raise CommandException("no current window")
        return window.tags
    return the_pane(pymux, args.target).tags


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("-s", dest="s", action="store_true", help="A session's tags.")
    scope.add_argument("-w", dest="w", action="store_true", help="A window's tags.")
    scope.add_argument("-p", dest="p", action="store_true", help="A pane's tags, which is the default.")
    scope.add_argument("-j", dest="j", action="store_true", help="A job's tags; -t names the job by its id.")
    parser.add_argument("-t", dest="target", metavar="<target>", help="The session, window, pane or job.")


def set_tag(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Tag a session, a window, a pane or a job.

    Each word is a bare key or `key=value`: a bare key carries no
    value, so `key` and `key=` stay two tags. A key already there takes
    the new value. With `-u` each word is a key, and it is taken off.
    A tag is the caller's own word about the thing, which `--tag` on
    `list-sessions`, `list-windows`, `list-panes` and `list-jobs`
    finds again. Lillecarl/pymux#762.
    """
    tags = tags_of(pymux, args)
    if args.u:
        for word in args.words:
            tags.pop(word, None)
        return
    for word in args.words:
        key, value = parse_tag(word)
        tags[key] = value


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, set_tag)
    add_scope_arguments(parser)
    parser.add_argument("-u", dest="u", action="store_true", help="Take the keys off instead.")
    parser.add_argument("words", metavar="<key[=value]>", nargs="+")
