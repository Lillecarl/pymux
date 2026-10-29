"""
`list-panes -s -t <session>` lists the panes of that session: with `-s`
the target is a session, as tmux reads it. Lillecarl/pymux#808.
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


async def test_with_s_the_target_is_a_session():
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        pymux = session.pymux
        made = await session.command("new-session -d -s other")
        await once(lambda: _said(made)[2] is not None, 10.0, "new-session never answered")
        other = next(s for s in pymux.sessions if s.name == "other")
        (window,) = other.arrangement.windows
        (pane,) = window.panes

        got = await session.command("list-panes -s -t other -F '#{pane_id}'")
        await once(lambda: _said(got)[2] is not None, 10.0, "list-panes never answered")
        assert _said(got) == ("%%%d\n" % pane.pane_id, "", 0)
