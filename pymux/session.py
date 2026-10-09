"""
A session: the windows somebody arranged, and the clients on them.

One server holds many of these. tmux says the same about itself -- "all
sessions are managed by a single server" -- and pymux held exactly one
until Lillecarl/pymux#323.
"""

from __future__ import annotations

import time
from typing import ClassVar

from prompt_toolkit.data_structures import Size
from pyte.keep import Keep

from .arrangement import Arrangement, Pane
from .ids import SessionId

__all__ = ["DEFAULT_SIZE", "Session"]

#: How big a window of this session is while no client is watching it.
#:
#: A window exists before a client attaches, and a program in a pane needs
#: a size from the first byte it writes. tmux answers the same two numbers
#: and calls them `default-size` (`cmd-new-session.c`, `dsx` and `dsy`).
DEFAULT_SIZE = Size(rows=24, columns=80)


class Session:
    """
    One session of a server.

    The windows are the session's. Which window a client looks at is the
    client's, and `Arrangement` keeps that per application.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "session_id": Keep.SAVED,
        "name": Keep.SAVED,
        "arrangement": Keep.SAVED,
        "environment": Keep.SAVED,
        "default_size": Keep.SAVED,
        "overlay_pane": Keep.SAVED,
        "overlay_title": Keep.SAVED,
        "overlay_width": Keep.SAVED,
        "overlay_height": Keep.SAVED,
        "created": Keep.SAVED,
        "last_used": Keep.SAVED,
    }

    def __init__(self, session_id: SessionId, name: str) -> None:
        #: The number in `$0`, the way tmux spells a session. It counts
        #: from zero for each server, and no two sessions of one server
        #: ever share it, including after one is killed.
        self.session_id = session_id

        self.name = name
        self.arrangement = Arrangement()

        #: What `set-environment` without `-g` fills. A new pane of this
        #: session reads this over the global environment.
        #: Lillecarl/pymux#270.
        self.environment: dict[str, str | None] = {}

        #: The overlay pane: a pane that floats in the middle of the
        #: screen over the layout, like the popup of tmux. It belongs
        #: to the session, so every client of this session sees the
        #: same one, and it takes the keyboard while it is open.
        #: Lillecarl/pymux#324.
        self.overlay_pane: Pane | None = None
        self.overlay_title = ""
        self.overlay_width: str | None = None
        self.overlay_height: str | None = None

        #: How big a window of this session is while nobody is watching
        #: it. `new-session -x -y` is what names it, and it is the only
        #: size a session that nothing ever attaches to has.
        #:
        #: **Not `window.manual_size`.** A manual size says a person
        #: means that size to stay, so a client that attaches keeps it
        #: and scrolls. This one gives way to the client, which is what
        #: `window-size` is for and what tmux does with the same flags.
        #: Lillecarl/pymux#459.
        self.default_size = DEFAULT_SIZE

        self.created = time.time()

        #: The turn of `Pymux.client_was_used` when a person last looked
        #: at this session. A client that attaches with no session named
        #: lands on the highest.
        self.last_used = 0

    def __repr__(self) -> str:
        return "Session($%s, %r)" % (self.session_id, self.name)
