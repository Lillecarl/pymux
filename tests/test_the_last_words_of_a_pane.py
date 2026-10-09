"""
What a program writes as it ends still reaches the client.

A program that writes a clipboard write and exits at once: the kernel
holds those bytes after the exit, and the pane's reader takes them on
the turns after it. If the pane went away first, the write would be
lost -- the race rmux fences with a drain before teardown.
Lillecarl/pymux#389.
"""

from __future__ import annotations

import anyio
from prompt_toolkit.application.current import set_app
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401 -- a fixture

#: A clipboard write, as a program writes one.
COPIED = "]52;c;aGVsbG8="


async def test_a_clipboard_write_a_program_ends_on_reaches_the_client(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state

        sent: list[str] = []
        take = session.take_packet

        def keep(packet):
            sent.append(str(packet.get("data", "")))
            return take(packet)

        session.take_packet = keep

        with set_app(client_state.app):
            pymux.handle_command("set-option set-clipboard on")
            pymux.handle_command("split-window \"printf '\\033%s\\007'\"" % COPIED)

        with anyio.fail_after(10):
            while not any(COPIED in data for data in sent):
                await anyio.sleep(0.02)
