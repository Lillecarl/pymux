"""Helpers the session commands share."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux
    from pymux.session import Session


from pymux.commands import CommandException, this_client


def find_session(pymux: Pymux, target: str | None) -> Session:
    """
    The session a `-t` names.

    No target means the session a person looked at last, which is what
    a session command with no `-t` means in tmux.
    """
    if not target:
        return pymux.last_used_session

    session = pymux.get_session(target)
    if session is None:
        raise CommandException("can't find session: %s" % (target,))
    return session


def move_this_client(
    pymux: Pymux,
    target: str | None,
    detach_others: bool = False,
    hang_up_others: bool = False,
) -> ClientState:
    """
    Put the calling client on the session a `-t` names, and answer
    with it.

    `hang_up_others` is `attach-session -x`: the same clients leave,
    and the terminals they were in close. It says nothing about
    whether the others go -- tmux reads `-x` as `-d` with a harsher
    message (`if (dflag || xflag)`, `cmd-attach-session.c:123`), and so
    does the caller. Lillecarl/pymux#347.

    Which clients those are is `Pymux.clients_on`, which the server
    reads for the same flag on the `start-gui` packet.
    Lillecarl/pymux#349.
    """
    session = find_session(pymux, target)

    client_state = this_client(pymux)
    if client_state is None:
        raise CommandException(
            "no client to move: this command did not come from an attached client."
        )

    if detach_others or hang_up_others:
        for other in pymux.clients_on(session, except_for=client_state):
            pymux.detach_client(other.app, hang_up=hang_up_others)

    pymux.attach_client_to(client_state, session)
    return client_state
