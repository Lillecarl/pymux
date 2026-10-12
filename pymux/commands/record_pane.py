from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from ptyhost.process import Process

from pymux.commands import CommandException, CommandParser, add_command
from pymux.commands.common import the_pane
from pymux.recording import Recording, recordings_directory

if TYPE_CHECKING:
    from pymux.main import Pymux


def record_pane(pymux: Pymux, args: argparse.Namespace) -> None:
    """
    Start recording a pane, or stop and write what was recorded.

    The files go to `~/.local/state/pymux/recordings`, and they are the
    three that `ptyhost-record` writes: `<name>.bin`, `<name>.reads.json`
    and `<name>.session.json`. Without a name the program's name and the
    time make one.
    """
    pane = the_pane(pymux, args.target_pane)
    if pane.recording is not None:
        recording, pane.recording = pane.recording, None
        try:
            written = recording.finish()
        except OSError as e:
            raise CommandException("record-pane: %s" % e)
        pymux.show_message("Recorded %s" % written)
    else:
        if not isinstance(pane.process, Process):
            raise CommandException("record-pane: this pane runs no program on a pty")
        screen = pane.screen
        pane.recording = Recording(pane.process, args.name or "", screen.lines, screen.columns, pymux.default_terminal)
        pymux.show_message("Recording %s to %s" % (pane.recording.name, recordings_directory()))
    pymux.invalidate()


def register(subparsers: argparse._SubParsersAction[CommandParser]):
    parser = add_command(subparsers, record_pane)
    parser.add_argument("-t", dest="target_pane", metavar="<target-pane>", help="The pane to record.")
    parser.add_argument("name", nargs="?", metavar="<name>", help="What the files are called.")
