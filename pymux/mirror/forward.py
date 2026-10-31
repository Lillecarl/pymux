"""
A command on a session another server owns runs on that server.

**The owner runs it as a command typed in its pane.** The peer resolves
the targets the command names, finds the session they are in, and sends
the words to the owner over a stream of its own: the targets in the
owner's ids, and the pane the person here is in, which the owner reads
as the caller's pane. So a command with no target acts where the person
is, the same as it does on the owner. Only commands marked `acts_on`
come here (`commands.add_command`); a command about what one client
looks at stays here. Lillecarl/pymux#759.

The person's client follows a command that moves the owner's caller to
another window, the way `new-window` moves the client that runs it. The
owner says where it left its caller, and the client moves there once a
tree brought that pane here.
"""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Coroutine, Mapping
from typing import TYPE_CHECKING, Any, cast

from prompt_toolkit.application.current import set_app

from ..answer import Answer
from ..commands import CommandException, Target, this_client
from ..commands.common import find_pane, find_window, session_of_target, session_part
from ..enums import Woke
from .program import RemoteProgram

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..core.main import Pymux
    from ..core.pane import Pane
    from ..core.session import Session
    from ..core.window_tree import Window
    from .peer import Peer

__all__ = ["forwarded"]

#: How long the client waits for the tree to bring the pane a forwarded
#: command left the owner's caller on. A tree follows a change within a
#: fraction of a second; one that does not come leaves the client where it is.
FOLLOW_WITHIN = 2.0


def forwarded(
    pymux: Pymux, command: str, arguments: list[str], namespace: argparse.Namespace
) -> Coroutine[Any, Any, None] | None:
    """
    The run of a command on the server that owns the session it acts
    on, or None when that is this server. A target nothing here holds
    is None too, so the command says it cannot find it.

    Refused when the command's targets are on two servers: a pane
    cannot move from one server to another.
    """
    acts_on: Mapping[str, Target] = namespace._acts_on
    named: list[tuple[str, Target, object, Session]] = []
    for dest, kind in acts_on.items():
        value = getattr(namespace, dest, None)
        if not value:
            continue
        found = _resolve(pymux, kind, value)
        if found is None:
            return None
        named.append((dest, kind, *found))

    sessions = [session for *_, session in named]
    caller = None
    if not acts_on or len(named) < len(acts_on):
        # A target left out means the caller's: its pane, or the window
        # or session that holds the pane.
        caller = find_pane(pymux, None)
        session = pymux.session_holding(caller) if caller is not None else None
        sessions.append(session or pymux.current_session)

    owners = {None if session.remote is None else session.remote.peer for session in sessions}
    if owners == {None}:
        return None
    if len(owners) > 1:
        raise CommandException("%s: its targets are on different servers" % (command,))
    (peer,) = owners
    assert peer is not None

    targets = {dest: _there(peer, kind, found, session) for dest, kind, found, session in named}
    caller_id = cast(RemoteProgram, caller.process).pane_id if caller is not None else None
    text = shlex.join([command, *arguments])
    # What the command answers to and who ran it, now: the coroutine
    # runs later, when another command may be current.
    return _run(pymux, peer, text, targets, caller, caller_id, pymux.answer, this_client(pymux))


def _resolve(pymux: Pymux, kind: Target, value: str) -> tuple[object, Session] | None:
    "What a target names here, and the session that holds it."
    if kind is Target.PANE:
        pane = find_pane(pymux, value)
        session = pymux.session_holding(pane) if pane is not None else None
        return None if session is None else (pane, session)
    if kind is Target.WINDOW:
        window = find_window(pymux, value)
        session = pymux.session_of_window(window) if window is not None else None
        return None if session is None else (window, session)
    if kind is Target.SLOT:
        session, rest = session_part(pymux, value)
        return None if session is None else (rest, session)
    try:
        session = session_of_target(pymux, value)
    except CommandException:
        return None
    return session, session


def _there(peer: Peer, kind: Target, found: object, session: Session) -> str:
    "A target resolved here, as the owner's ids spell it."
    if kind is Target.PANE:
        return "%%%d" % (cast(RemoteProgram, cast("Pane", found).process).pane_id,)
    if kind is Target.WINDOW:
        window_id = peer.tree.id_of_window(cast("Window", found))
        if window_id is None:
            raise CommandException("the window is gone from %s" % (peer.address,))
        return "@%d" % (window_id,)
    assert session.remote is not None
    if kind is Target.SLOT:
        return "$%d:%s" % (session.remote.session_id, found)
    return "$%d" % (session.remote.session_id,)


async def _run(
    pymux: Pymux,
    peer: Peer,
    text: str,
    targets: dict[str, str],
    caller: Pane | None,
    caller_id: int | None,
    asker: Answer | None,
    client: ClientState | None,
) -> None:
    result = await peer.run_command(text, caller_id, targets)
    if result.exit_code != 0:
        raise CommandException(result.stderr.strip() or "%s failed on %s" % (text, peer.address))
    if result.stdout:
        if asker is not None:
            asker.output.extend(result.stdout.splitlines())
        elif client is not None:
            client.message = result.stdout.strip()
    if asker is not None:
        for line in result.stderr.splitlines():
            asker.warn(line)
    if client is not None and caller is not None and result.pane_id is not None:
        await _follow(pymux, client, peer, caller, result.pane_id)


async def _follow(pymux: Pymux, client: ClientState, peer: Peer, caller: Pane, pane_id: int) -> None:
    "Move the client to the window the owner left its caller in, when that is another one."
    pane = await peer.tree.pane_once_here(pane_id, FOLLOW_WITHIN)
    if pane is None or pane is caller:
        return
    window = pymux._window_holding(pane)
    if window is None or window is pymux._window_holding(caller):
        return
    session = pymux.session_of_window(window)
    if session is None or session is not client.session:
        return
    with set_app(client.app):
        session.arrangement.set_active_window(window)
    pymux.invalidate(Woke.TREE_MIRRORED)
