"""
`list-sessions` lists a session that holds no window, with its window
and pane variables empty, as tmux does. A session is empty before its
first window and after its last. Lillecarl/pymux#810.
"""

from __future__ import annotations

import pytest
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


@pytest.mark.parametrize(
    ("flags", "empty"),
    [
        ("-F '#{session_name} #{window_index} #{pane_id}'", "empty  "),
        ("-F '#S #I #W #P'", "empty   "),
        ("-J '{{ session_name }} {{ window_name }} {{ pane_tags }}'", "empty  {}"),
    ],
)
async def test_a_session_with_no_window_is_listed_with_its_windows_variables_empty(flags, empty):
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        pymux = session.pymux
        await _run(session, "new-session -d -s empty")
        held = next(s for s in pymux.sessions if s.name == "empty")
        held.arrangement.windows.clear()

        out, err, code = await _run(session, "list-sessions %s" % (flags,))
        assert (err, code) == ("", 0)
        assert empty in out.splitlines()
        assert len(out.splitlines()) == 2
