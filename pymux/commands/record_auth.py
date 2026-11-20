from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

from pymux.asciinema import UploadError, connect_url
from pymux.commands import CommandException, CommandParser, add_command

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def record_auth(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Print the link that ties this machine's uploads to an asciinema account.

    It is what `asciinema auth` prints, from the same install id: open it
    while logged in, and every `record-upload` from here, and every
    upload of the CLI, lands in that account. The link holds the id an
    upload proves itself with, so it is printed to the caller alone and
    never shown on a screen. Lillecarl/pyterm#1064.
    """
    if pymux.answer is None:
        raise CommandException("record-auth: run it from a shell, so the link is not shown on a screen")
    try:
        url = connect_url(os.environ)
    except (OSError, UploadError) as e:
        raise CommandException("record-auth: %s" % e)
    pymux.print_command_line(url)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    add_command(subparsers, record_auth)
