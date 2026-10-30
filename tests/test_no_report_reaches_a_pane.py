"""
What iTerm2's stuck-state banners look for: a report from the terminal
that reached a program as text. Lillecarl/pymux#792.

The pane's program asks for no mouse, no bracketed paste and no in-band
resize, and writes every byte it reads to a file. The client sends what
iTerm2 sends, packed the way `record-client` found it: a mouse report in
the same packet as the pacing answer.
"""

from __future__ import annotations

import shlex

import pytest
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.core.main import Pymux

SIZE = Size(rows=24, columns=80)


SENT = (
    "\x1b[0n\x1b[<35;10;5M",
    "\x1b[<0;10;5M\x1b[<0;10;5m",
    "\x1b[48;24;80;480;800t",
    "\x1b[200~hi\x1b[201~",
    "z",
)


@pytest.mark.parametrize("packets", [SENT, tuple("".join(SENT))], ids=["as recorded", "a byte at a time"])
async def test_no_report_reaches_a_program_that_did_not_ask(tmp_path, packets):
    heard = tmp_path / "heard"
    command = "sh -c %s" % shlex.quote("stty raw -echo; exec cat > %s" % shlex.quote(str(heard)))
    async with over_connection(pymux=Pymux(startup_command=command)) as session:
        state, _ = await session.attach("one", SIZE)
        await once(lambda: heard.exists(), 10.0, "the pane never started")
        for data in packets:
            state.connection._process({"cmd": "in", "data": data})
        await once(lambda: heard.read_bytes().endswith(b"z"), 10.0, lambda: "heard %r" % heard.read_bytes())
        assert heard.read_bytes() == b"hiz"
