from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from prompt_toolkit.data_structures import Size

from pymux.commands import CommandException, CommandParser, Target, add_command, add_target
from pymux.commands.record_pane import seconds
from pymux.connection.recorder import recorder_of, start_recorder
from pymux.connection.recording import a_name

if TYPE_CHECKING:
    from pymux.core.main import Pymux


def record_session(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Start recording a session with no terminal attached, or stop.

    A client with no terminal attaches to the session and records what
    pymux draws for it, with no person and no terminal needed: what an
    agent runs to record a demo. The files are the ones `record-client`
    writes, and `record-upload` sends them to asciinema. Run it again to
    stop, and it prints where the recording went. Lillecarl/pyterm#1064.

    **No pane a person watches moves.** With nobody watching, the
    recorder keeps the windows at the size they have, or at `-x` and
    `-y`, also after a person attaches. With somebody watching, it takes
    their size and shows what they see. `-F` records with no status
    line and no pane borders.

    **The recording keeps the demo's time, not the agent's.** A pause
    longer than `-i` seconds, 1.5 unless this says otherwise, is cut to
    it as it happens: an agent thinks between two keys, and a player
    would show a person staring. `--hold <seconds>` on a session being
    recorded makes the next pause that long, so a screen stays up for a
    viewer to read whatever the agent's pace.
    """
    session = pymux.current_session if args.target_session is None else pymux.get_session(args.target_session)
    if session is None:
        raise CommandException("can't find session: %s" % (args.target_session,))
    recorder = recorder_of(pymux, session)
    if args.hold is not None:
        if recorder is None or recorder.client.recording is None:
            raise CommandException("record-session: session %s is not being recorded" % session.name)
        recorder.client.recording.hold(args.hold)
        return
    if recorder is not None:
        client = recorder.client
        recording, client.recording = client.recording, None
        client.connection.detach_and_close()
        if recording is None:
            return
        try:
            written = recording.finish()
        except OSError as e:
            raise CommandException("record-session: %s" % e)
        pymux.print_command_line(str(written))
        pymux.show_message("Recorded %s" % written)
        return

    if (args.width is None) != (args.height is None):
        raise CommandException("record-session: -x and -y go together")
    size = Size(rows=args.height, columns=args.width) if args.width is not None else None
    name = args.name or a_name("session-%s" % session.name)
    start_recorder(
        pymux,
        session,
        name,
        size=size,
        pause_cap=args.idle_time_limit,
        full_screen=args.full_screen,
    )
    pymux.print_command_line(name)


def cells(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 2 <= value <= 1000:
        raise argparse.ArgumentTypeError("a size is between 2 and 1000 cells")
    return value


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_session)
    add_target(parser, "-t", kind=Target.SESSION, dest="target_session", help="The session to record.")
    parser.add_argument("-x", dest="width", type=cells, metavar="<columns>", help="The width, with -y.")
    parser.add_argument("-y", dest="height", type=cells, metavar="<rows>", help="The height, with -x.")
    parser.add_argument(
        "-i", dest="idle_time_limit", type=seconds, metavar="<seconds>", help="The longest pause a player shows."
    )
    parser.add_argument(
        "--hold",
        type=seconds,
        metavar="<seconds>",
        help="On a session being recorded: the next pause lasts this long.",
    )
    parser.add_argument("-F", dest="full_screen", action="store_true", help="No status line and no pane borders.")
    parser.add_argument("name", nargs="?", metavar="<name>", help="What the files are called.")
