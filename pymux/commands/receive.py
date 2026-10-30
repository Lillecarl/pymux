from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane
    from pymux.main import Pymux
    from pymux.server import ServerConnection
    from pymux.uploads import Upload


from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import answer, find_pane, refuse_without_a_waiter
from pymux.enums import FileTransfer
from pymux.uploads import receive_into


def receive(pymux: Pymux, args: argparse.Namespace):
    """
    Get files from the machine the person sits at.

    The pymux client there asks the person first, on its own terminal,
    unless a grant it holds or its own `upload-paths` lets the paths
    pass. Each file lands in the directory -d names, by default the
    one this command runs in, and never over a file that is there.
    The answer says where each one went, or why it did not.
    Lillecarl/pymux#743.
    """
    refuse_without_a_waiter(pymux, "wait")
    pane, connection = _sender(pymux)

    caller = pymux.caller
    here = caller.cwd if caller is not None else None
    directory = Path(here or os.getcwd()) / (args.directory or "")
    if not directory.is_dir():
        raise CommandException("%s is not a directory." % (directory,))

    upload = connection.ask_for_files(list(args.paths), "%%%d" % (pane.pane_id,))
    return wait_for_files(pymux, connection, upload, directory)


def _sender(pymux: Pymux) -> tuple[Pane, ServerConnection]:
    "The caller's pane, and the connection of the client that sends its files."
    pane = find_pane(pymux, None)
    if pane is None:
        raise CommandException("There is no pane to ask from.")
    client = pymux.outward.download_client(pane)
    if client is None:
        raise CommandException("Nobody is attached to the session of this pane, so nobody can send a file.")
    if client.options.file_transfer is FileTransfer.PASSTHROUGH:
        raise CommandException("file-transfer is passthrough, and only the pymux client sends a file.")
    connection = client.connection
    if not connection.sends_files:
        raise CommandException("This pymux client is too old to send a file; start a new one.")
    return pane, connection


def list_person_files(pymux: Pymux, args: argparse.Namespace):
    """
    List a directory on the person's machine, for completing `receive`.

    One name a line, a directory's with `/` after it. The client answers inside a grant that `a` gave, and otherwise only
    with the names that lead into its `upload-paths`. It never asks the
    person. Lillecarl/pymux#748.
    """
    refuse_without_a_waiter(pymux, "wait")
    _pane, connection = _sender(pymux)
    return _print_listing(pymux, connection, args.directory)


async def _print_listing(pymux: Pymux, connection: ServerConnection, directory: str) -> None:
    for name in await connection.list_files(directory):
        answer(pymux, name)


async def wait_for_files(pymux: Pymux, connection: ServerConnection, upload: Upload, directory: Path) -> None:
    try:
        failed = await receive_into(upload, directory, lambda text: answer(pymux, text))
    finally:
        connection.stop_waiting_for_files(upload)
    if failed:
        raise CommandException("\n".join("Did not receive %s." % (why,) for why in failed))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, receive)
    parser.add_argument(
        "-d",
        dest="directory",
        metavar="<directory>",
        default=None,
        help="Write the files here. The directory this command runs in otherwise.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="<path>",
        help="A file on the person's machine. ~ is their home there, and so is a path that is not absolute.",
    )

    parser = add_command(subparsers, list_person_files)
    parser.add_argument(
        "directory",
        nargs="?",
        default="~",
        metavar="<directory>",
        help="A directory on the person's machine, ~ unless given.",
    )
