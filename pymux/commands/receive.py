from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import Pymux
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
    pane = find_pane(pymux, None)
    if pane is None:
        raise CommandException("There is no pane to ask from.")
    client = pymux.download_client(pane)
    if client is None:
        raise CommandException("Nobody is attached to the session of this pane, so nobody can send a file.")
    if client.file_transfer is FileTransfer.PASSTHROUGH:
        raise CommandException("file-transfer is passthrough, and only the pymux client sends a file.")
    connection = client.connection
    if not connection.sends_files:
        raise CommandException("This pymux client is too old to send a file; start a new one.")

    caller = pymux.caller_context
    here = caller.cwd if caller is not None else None
    directory = Path(here or os.getcwd()) / (args.directory or "")
    if not directory.is_dir():
        raise CommandException("%s is not a directory." % (directory,))

    upload = connection.ask_for_files(list(args.paths), "%%%d" % (pane.pane_id,))
    return _wait(pymux, connection.uploads, upload, directory)


async def _wait(pymux: Pymux, uploads: dict[str, Upload], upload: Upload, directory: Path) -> None:
    try:
        failed = await receive_into(upload, directory, lambda text: answer(pymux, text))
    finally:
        uploads.pop(upload.request, None)
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
