"""
Killing a session's last pane ends the session, as tmux does.
Lillecarl/pymux#807.
"""

from __future__ import annotations

from prompt_toolkit.data_structures import Size
from session import once, over_connection

SIZE = Size(rows=24, columns=80)


def _said(got: list) -> tuple[str, str, int | None]:
    out = "".join(packet.get("data", "") for packet in got if packet.get("cmd") == "out")
    err = "".join(packet.get("data", "") for packet in got if packet.get("cmd") == "err")
    codes = [packet.get("code") for packet in got if packet.get("cmd") == "exit"]
    return out, err, codes[0] if codes else None


async def _run(session, command: str) -> tuple[str, str, int | None]:
    got = await session.command(command)
    await once(lambda: _said(got)[2] is not None, 10.0, "%s never answered" % (command,))
    return _said(got)


async def test_killing_the_last_pane_ends_the_session():
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        pymux = session.pymux
        await _run(session, "new-session -d -s other")
        other = next(s for s in pymux.sessions if s.name == "other")
        (pane,) = other.arrangement.windows[0].panes

        await _run(session, "kill-pane -t %%%d" % pane.pane_id)
        await once(lambda: other not in pymux.sessions, 10.0, lambda: "still there: %r" % (pymux.sessions,))


async def test_killing_a_pane_that_already_ended_ends_the_session():
    "Under `remain-on-exit` the end was heard before the kill, and nothing calls back again."
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        pymux = session.pymux
        await _run(session, "set-option -g remain-on-exit on")
        await _run(session, "new-session -d -s other 'true'")
        other = next(s for s in pymux.sessions if s.name == "other")
        (pane,) = other.arrangement.windows[0].panes
        await once(lambda: pane.process.is_terminated, 10.0, "the program never ended")

        await _run(session, "kill-pane -t %%%d" % pane.pane_id)
        await once(lambda: other not in pymux.sessions, 10.0, lambda: "still there: %r" % (pymux.sessions,))
