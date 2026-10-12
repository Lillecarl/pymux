from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from ptyhost.asciicast import asciicast

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.record_pane import seconds
from pymux.recording import recordings_directory
from pymux.web.replay import replay_of

if TYPE_CHECKING:
    from pymux.main import Pymux

#: What each format writes, beside the recording.
SUFFIXES = {"web": ".web.json", "asciicast": ".cast"}


def the_session_file(name: str) -> Path:
    "A recording by name, in the recordings directory, or by the path of its `.session.json`."
    if name.endswith(".session.json"):
        return Path(name).expanduser()
    return recordings_directory() / ("%s.session.json" % name)


def export(session_file: Path, form: str, idle_time_limit: float | None, cast_version: int) -> Path:
    "Read one recording and write it in `form`. Gives back what it wrote."
    recording = json.loads(session_file.read_text())
    if form == "web":
        text = json.dumps(replay_of(recording, idle_time_limit), separators=(",", ":"))
    else:
        text = asciicast(recording, cast_version, idle_time_limit)
    output = session_file.with_name(session_file.name.removesuffix(".session.json") + SUFFIXES[form])
    output.write_text(text)
    return output


async def record_export(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Write a recording of `record-pane` or `record-client` in another format.

    `-F web` writes `<name>.web.json`: the frames `<pymux-pane>` draws,
    each at the time it came, for a web player. `-F asciicast` writes
    `<name>.cast`, which asciinema plays and asciinema.org takes; it is
    version 3 unless `--cast-version 2` says otherwise. An asciicast
    player knows nothing of images or of pymux's other extensions.

    `-i` is the longest pause a player shows, and replaces the one the
    recording was made with. Lillecarl/pymux#610.
    """
    session_file = the_session_file(args.name)
    if not session_file.exists():
        raise CommandException("record-export: no recording %s" % session_file)
    try:
        # A long recording takes a while to draw, and the server goes on
        # drawing for everybody meanwhile.
        written = await anyio.to_thread.run_sync(
            export, session_file, args.format, args.idle_time_limit, args.cast_version
        )
    except (OSError, ValueError, KeyError) as e:
        raise CommandException("record-export: %s" % e)
    pymux.show_message("Exported %s" % written)


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_export)
    parser.add_argument(
        "-F",
        dest="format",
        choices=sorted(SUFFIXES),
        default="web",
        help="What to write: web (the default) or asciicast.",
    )
    parser.add_argument(
        "-i", dest="idle_time_limit", type=seconds, metavar="<seconds>", help="The longest pause a player shows."
    )
    parser.add_argument(
        "--cast-version", type=int, choices=(2, 3), default=3, help="The asciicast version, 3 unless this says 2."
    )
    parser.add_argument("name", metavar="<name>", help="The recording, by name or by its .session.json.")
