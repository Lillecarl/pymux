"""
A missed SSH keepalive is a link that went away, not an answer from the
far side, so the peer dials again. Lillecarl/pyterm#979.
"""

from __future__ import annotations

import anyio
import asyncssh
from test_a_server_mirrors_a_server import the_probe_does_not_leave_this_machine, two_servers, until
from test_forwards_through_a_peer import linked_over_ssh

__all__ = ["the_probe_does_not_leave_this_machine", "two_servers"]


async def test_a_missed_keepalive_dials_again(two_servers, tmp_path):
    owner, peer, client = two_servers
    with anyio.fail_after(20):
        async with linked_over_ssh(owner, peer, client, tmp_path) as (here, _there):
            (link,) = owner.peer_links()
            # What asyncssh does when the keepalives go unanswered.
            here.stream.connection.connection_lost(asyncssh.ConnectionLost("Server not responding to keepalive"))
            await until(
                lambda: not here.running or any(new is not link for new in owner.peer_links()),
                "the peer dials again or gives up",
            )
            assert here.running, here.error
            assert here.error is None or "TaskGroup" not in str(here.error), here.error
