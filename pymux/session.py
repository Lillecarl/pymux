"""
A session: the windows somebody arranged, and the clients on them.

One server holds many of these. tmux says the same about itself -- "all
sessions are managed by a single server" -- and pymux held exactly one
until Lillecarl/pymux#323.
"""

import time
from typing import Dict

from .arrangement import Arrangement

__all__ = ["Session"]


class Session:
    """
    One session of a server.

    The windows are the session's. Which window a client looks at is the
    client's, and `Arrangement` keeps that per application.
    """

    def __init__(self, session_id: int, name: str) -> None:
        #: The number in `$0`, the way tmux spells a session. It counts
        #: from zero for each server, and no two sessions of one server
        #: ever share it, including after one is killed.
        self.session_id = session_id

        self.name = name
        self.arrangement = Arrangement()

        #: What `set-environment` without `-g` fills. A new pane of this
        #: session reads this over the global environment.
        #: Lillecarl/pymux#270.
        self.environment: Dict[str, str | None] = {}

        #: The overlay pane: a pane that floats in the middle of the
        #: screen over the layout, like the popup of tmux. It belongs
        #: to the session, so every client of this session sees the
        #: same one, and it takes the keyboard while it is open.
        #: Lillecarl/pymux#324.
        self.overlay_pane = None
        self.overlay_title = ""
        self.overlay_width: str | None = None
        self.overlay_height: str | None = None

        self.created = time.time()

        #: The turn of `Pymux.client_was_used` when a person last looked
        #: at this session. A client that attaches with no session named
        #: lands on the highest.
        self.last_used = 0

    def __repr__(self) -> str:
        return "Session($%s, %r)" % (self.session_id, self.name)
