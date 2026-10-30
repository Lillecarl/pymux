"""
The environment a pane runs under: the server's, what `set-environment -g`
and `set-environment` put over it, and the names a client brings when it
attaches (`update-environment`). Lillecarl/pymux#270, Lillecarl/pymux#271.
"""

from __future__ import annotations

import fnmatch
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from .main import Pymux

__all__ = ["pane_environment", "take_environment_from"]


def take_environment_from(pymux: Pymux, client_state: ClientState) -> None:
    """
    Refresh the named variables of a session from the client that
    just attached to it. ("update-environment".)

    **The machine a person attaches from is the machine their panes
    should talk to.** `DISPLAY`, `SSH_AUTH_SOCK` and `WAYLAND_DISPLAY`
    name things that live on that machine, and a server outlives the
    client that started it: over ssh (Lillecarl/pymux#90) an old
    `DISPLAY` from the server's own start is worse than none, because
    a pane then opens a window nobody is looking at.

    A name the client does not carry is taken out of the session
    rather than left behind, which is what tmux does
    (`environ_update` in its `environ.c`) and for the same reason:
    the point is that these follow the client.

    `set-environment` writes the same dictionary and is not touched
    here, except for the names this option lists -- those belong to
    whoever attached. Lillecarl/pymux#271, Lillecarl/pymux#270.
    """
    reported = client_state.connection.environment
    environment = client_state.session.environment

    for pattern in client_state.session.options.update_environment.split():
        # A pattern, because tmux matches one: `XDG_*` is how a
        # person takes a family of them without naming each.
        found = False
        for name, value in reported.items():
            if fnmatch.fnmatchcase(name, pattern):
                environment[name] = value
                found = True

        if not found:
            environment.pop(pattern, None)


def pane_environment(pymux: Pymux, base: dict[str, str] | None = None) -> dict[str, str]:
    """
    The environment a new pane runs under, merged: `base`, or the
    server's own, with what `set-environment -g` set and then what
    a plain `set-environment` set over it. An unset takes the name out of
    its scope only: a session unset of a name the global set
    falls through to the global value, and a name no scope says
    anything about leaves entirely. Lillecarl/pymux#270.
    """
    merged = dict(os.environ if base is None else base)
    for name, value in pymux.global_environment.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    for name, value in pymux.session_environment.items():
        if value is None:
            # A session unset does not reach past the global
            # scope: what the global set stays, and a name the
            # global says nothing about leaves.
            if name not in pymux.global_environment:
                merged.pop(name, None)
        else:
            merged[name] = value
    return merged
