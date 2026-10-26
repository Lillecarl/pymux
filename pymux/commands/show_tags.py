from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux


from pymux.commands import CommandParser, add_command
from pymux.commands.common import answer
from pymux.commands.set_tag import add_scope_arguments, tags_of


def show_tags(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Print the tags of a session, a window, a pane or a job.

    One to a line, sorted by key: `key=value`, or the key alone for a
    bare key. `set-tag` takes the same flags and sets them.
    """
    tags = tags_of(pymux, args)
    answer(pymux, "\n".join(key if value is None else "%s=%s" % (key, value) for key, value in sorted(tags.items())))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, show_tags, read_only=True)
    add_scope_arguments(parser)
