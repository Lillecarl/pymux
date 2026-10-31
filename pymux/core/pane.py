"""
One pane: a terminal, and what pymux knows about it beside the screen.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, ClassVar

from prompt_toolkit.application import Application, get_app
from ptterm import Terminal
from pyte.keep import Keep

from ..downloads import DownloadReader
from ..ids import PaneId
from ..notify.osc import NO_PROGRESS

if TYPE_CHECKING:
    from ..connection.recording import Recording
    from ..tags import Tags
    from ..wayland.window_pane import WindowWidget


__all__ = ["Pane"]


class Pane:
    """
    One pane, containing one process and a search buffer for going into copy
    mode or displaying the help.
    """

    _pane_counter = 1000  # Start at 1000, to be sure to not confuse this with pane indexes.

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The counters of panes, windows and splits are class attributes,
    #: so no instance walk sees them: a snapshot saves them too, or a
    #: new id collides with one that survived. Lillecarl/pymux#399.
    KEEP: ClassVar[dict[str, Keep]] = {
        "pane_id": Keep.SAVED,
        "terminal": Keep.SAVED,
        "chosen_name": Keep.SAVED,
        "clock_mode": Keep.SAVED,
        "user_vars": Keep.SAVED,
        "tags": Keep.SAVED,
        "current_directory": Keep.SAVED,
        "current_host": Keep.SAVED,
        "current_user": Keep.SAVED,
        "shell": Keep.SAVED,
        "shell_integration_version": Keep.SAVED,
        # A program says its progress again as it moves.
        "progress": Keep.DROPPED,
        "command_zone": Keep.SAVED,
        "last_exit_status": Keep.SAVED,
        "bell": Keep.SAVED,
        # `#{pane_revision}` only grows: an agent waiting on a change
        # holds a number from before the upgrade.
        "revision": Keep.SAVED,
        # An upgrade writes it out first: `upgrade.upgrade`.
        "recording": Keep.DROPPED,
        # A file half sent is lost; its sender sends it again.
        "downloads": Keep.DROPPED,
    }

    def __init__(self, terminal: Terminal | WindowWidget) -> None:
        self.terminal = terminal
        self.chosen_name: str | None = None

        # Displayed the clock instead of this pane content.
        self.clock_mode = False

        # Give unique ID.
        Pane._pane_counter += 1
        self.pane_id: PaneId = PaneId(Pane._pane_counter)

        #: How many times anything this pane shows may have changed. It
        #: only goes up, and `#{pane_revision}` is it.
        #:
        #: **`PaneMaker.create_pane` is what makes it move**, because it is
        #: what builds the terminal that says so. A pane built around
        #: anything else -- a dozen tests build one around a stub -- has
        #: the field and never counts, which is what lets those tests go
        #: on knowing nothing about a widget.
        #:
        #: **Compare it, never order it.** Two revisions that differ say
        #: something may have changed; a difference of one says nothing
        #: about how much. The count moves on output that draws nothing
        #: at all, because `ptyhost` calls `invalidate` after every read
        #: whatever the bytes were, and that is the safe direction: a
        #: reader that redraws for nothing loses a frame, and one that
        #: misses a change shows the wrong screen until the next one.
        #:
        #: **It is not `screen.writes`.** That counts writes to rows, so
        #: it stands still through a DECSCNM, an "OSC 4" and the death of
        #: the program -- each of which changes what a reader draws. A
        #: reader inside the server compares those itself, the way
        #: `ptterm` compares reverse video; a reader that has only this
        #: number cannot. Lillecarl/pymux#387.
        self.revision = 0

        #: Where the program says it is, from "OSC 7" or "OSC 1337 ;
        #: CurrentDir". A shell reports it on every prompt, and nothing
        #: else sets it. `None` is a pane nobody asked about yet.
        self.current_directory: str | None = None

        #: The machine the directory sits on, from the host of the
        #: "OSC 7" file URL. `None` is the machine here or a report
        #: that named no host. "OSC 1337 ; RemoteHost" sets it too.
        self.current_host: str | None = None

        #: The user the shell runs as there, from "OSC 1337 ; RemoteHost".
        self.current_user: str | None = None

        #: The shell and the version of its iTerm2 integration, from
        #: "OSC 1337 ; ShellIntegrationVersion". Lillecarl/pymux#784.
        self.shell: str | None = None
        self.shell_integration_version: str | None = None

        #: What the program says of its progress, from "OSC 9 ; 4".
        #: Lillecarl/pymux#788.
        self.progress = NO_PROGRESS

        #: What the shell published about itself, from "OSC 1337 ;
        #: SetUserVar". iTerm2 reads these for badges and triggers and
        #: WezTerm for titles; here they wait for a reader, the way the
        #: notification hub waited for its chooser.
        self.user_vars: dict[str, str] = {}

        #: What a caller said about this pane, with `set-tag`.
        self.tags: Tags = {}

        #: The shell integration zone the program stands in, from
        #: "OSC 133": "A" a prompt, "B" a command, "C" its output.
        #: `None` is outside every zone or after the command ended.
        self.command_zone: str | None = None

        #: The exit status the last command finished with, from
        #: "OSC 133 ; D ; status". `None` is no finished command yet,
        #: or one that was aborted.
        self.last_exit_status: int | None = None

        #: The program rang the bell where nobody saw it: in a pane that
        #: was not the active one of a window a client looks at. Making
        #: the pane active clears it. `#{pane_bell_flag}` is it.
        #: Lillecarl/pymux#501.
        self.bell = False

        #: What `record-pane` is recording of this pane, or None.
        self.recording: Recording | None = None

        #: The files this pane sends to the person, as their parts
        #: arrive. Lillecarl/pymux#743.
        self.downloads = DownloadReader()

    def content_changed(self) -> None:
        "Say that what this pane shows may have changed."
        self.revision += 1

    @property
    def is_copying(self) -> bool:
        """
        Is the client that runs now reading the history of this pane
        rather than the program in it?

        **The widget knows, and a pane asks it.** A flag of the pane's
        own went out of step with the widget once, and five readers of
        it all answered "no". Lillecarl/pymux#133.

        **Copy mode is a client's, not the pane's.** Each client that
        shows the pane is an application of its own, and the program
        runs on under all of them. Lillecarl/pymux#470.
        """
        return self.terminal.is_copying

    def is_copying_for(self, app: Application[Any] | None) -> bool:
        "Is the client of `app` reading the history of this pane?"
        return self.terminal.copy_views.of(app) is not None

    @property
    def process(self):
        return self.terminal.process

    @property
    def screen(self):
        "What the program in this pane has drawn."
        return self.terminal.screen

    @property
    def name(self) -> str:
        """
        The name for the window as displayed in the title bar and status bar.
        """
        # Name, explicitely set for the pane.
        if self.chosen_name:
            return self.chosen_name
        # Name from the process running inside the pane.
        name = self.process.get_name()
        if name:
            return os.path.basename(name)

        return ""

    def current_path(self) -> str:
        """
        Where the pane says it is, else where its process is.

        The same order a new pane starts in (`_directory_to_start_in`), so
        `split-window -c "#{pane_current_path}"` and a bare `split-window`
        agree. Over ssh the process table sees only the local ssh.
        """
        if self.current_directory:
            return self.current_directory
        try:
            return self.process.get_cwd() or ""
        except Exception:
            return ""

    def enter_copy_mode(self) -> None:
        "Open copy mode on this pane, for the client that runs now."
        self.terminal.enter_copy_mode()

    def focus(self) -> None:
        """
        Focus this pane.
        """
        get_app().layout.focus(self.terminal)
