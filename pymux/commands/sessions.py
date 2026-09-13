"""Helpers the session commands share."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux
    from pymux.session import Session


from pymux.commands import CommandException


def find_session(pymux: "Pymux", target: str | None) -> "Session":
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


def this_client(pymux: "Pymux") -> "ClientState | None":
    """
    The client that ran this command, or None when no person did.

    A command that arrived over the socket runs under a client that
    draws nothing and goes away with the answer. Moving that one moves
    nobody.
    """
    try:
        client_state = pymux.get_client_state()
    except ValueError:
        return None

    return None if client_state.temporary else client_state


def move_this_client(
    pymux: "Pymux", target: str | None, detach_others: bool = False
) -> None:
    "Put the calling client on the session a `-t` names."
    session = find_session(pymux, target)

    client_state = this_client(pymux)
    if client_state is None:
        raise CommandException(
            "no client to move: this command did not come from an attached client."
        )

    if detach_others:
        for other in list(pymux._client_states.values()):
            if other is not client_state and not other.temporary:
                if other.session is session:
                    pymux.detach_client(other.app)

    pymux.attach_client_to(client_state, session)
