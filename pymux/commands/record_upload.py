from __future__ import annotations

import argparse
import json
import os
from typing import TYPE_CHECKING

import anyio
from ptyhost.asciicast import asciicast

from pymux import __version__
from pymux.asciinema import Upload, UploadError, Visibility, upload
from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.record_export import the_session_file
from pymux.commands.record_pane import seconds

if TYPE_CHECKING:
    from pymux.core.main import Pymux


async def record_upload(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Upload a recording to asciinema.org, or the server asciinema is set to.

    It sends the recording as an asciicast, the way `record-export -F
    asciicast` writes it, and prints the URL the server gives back. The
    server and the account are the `asciinema` CLI's own: the same
    config files and the same install id, so a machine `asciinema auth`
    linked uploads to that account. `record-auth` gives the link that
    does it. Lillecarl/pyterm#1064.

    **The keys stay out unless `--keys` says so**, as in the CLI: a
    typed password is a key too.
    """
    session_file = the_session_file(args.name)
    try:
        recording = json.loads(await anyio.Path(session_file).read_text())
        cast = await anyio.to_thread.run_sync(asciicast, recording, 3, args.idle_time_limit, args.keys)
    except FileNotFoundError:
        raise CommandException("record-upload: no recording %s" % session_file)
    except (OSError, ValueError, KeyError) as e:
        raise CommandException("record-upload: %s" % e)
    what = Upload(
        cast=cast,
        filename=session_file.name.removesuffix(".session.json") + ".cast",
        title=args.title,
        description=args.description,
        visibility=args.visibility,
    )
    try:
        url = await upload(what, os.environ, "pymux/%s" % __version__)
    except UploadError as e:
        raise CommandException("record-upload: %s" % e)
    pymux.print_command_line(url)
    pymux.show_message("Uploaded %s to %s" % (session_file.name.removesuffix(".session.json"), url))


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_upload)
    parser.add_argument("-T", dest="title", metavar="<title>", help="The recording's title on the server.")
    parser.add_argument("-d", dest="description", metavar="<text>", help="Its description, in markdown.")
    parser.add_argument(
        "-v",
        dest="visibility",
        type=Visibility,
        choices=list(Visibility),
        help="Who sees it: public, unlisted or private. The server's default otherwise.",
    )
    parser.add_argument(
        "-i", dest="idle_time_limit", type=seconds, metavar="<seconds>", help="The longest pause a player shows."
    )
    parser.add_argument("--keys", action="store_true", help="Send the keys that were typed too.")
    parser.add_argument("name", metavar="<name>", help="The recording, by name or by its .session.json.")
