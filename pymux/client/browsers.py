"""
The browsers a client starts on its own machine for the server.

A program on the server drives one through `browser.open`, and the URL
a pane opens goes to this machine's browser too. Lillecarl/pymux#653.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import TYPE_CHECKING, Any

from libpymux.protocol import Field, Message, Packet

from . import browser

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .terminal import TerminalClient

__all__ = ["Browsers"]


class Browsers:
    "The browsers a client started, by launch."

    def __init__(self, client: TerminalClient) -> None:
        self.client = client
        #: The process and the profile of each browser, by launch.
        self.held: dict[str, Any] = {}

    def start(self, packet: Message) -> None:
        """
        Start the browser a program on the server asked for, and say
        where it listens, or why it does not. This machine is the
        server's too, so the port needs no forward.
        """
        launch = packet[Field.BROWSER]
        try:
            process, path, kind, profile = browser.start(
                packet.get(Field.PROGRAM) or "",
                packet.get(Field.PROFILE) or "",
                packet[Field.PORT],
                packet.get(Field.ARGUMENTS) or [],
            )
        except (browser.BrowserError, OSError) as error:
            self.client.send_packet({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.ERROR: str(error)})
            return
        self.held[launch] = (process, profile)
        self.client.send_packet(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: path, Field.REMOTE: kind}
        )

    async def run(self, packet: Message, forward: Callable[[int], Awaitable[str | None]] | None = None) -> None:
        """
        `start` for a client with an event loop, which holds the browser
        until it ends and then says so. `forward(port)`, when given,
        opens the way to it from the server's machine and answers why
        not, or None.
        """
        launch, port = packet[Field.BROWSER], packet[Field.PORT]

        def failed(why: str) -> None:
            self.client.send_packet({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.ERROR: why})

        try:
            process, path, kind, profile = await browser.start_async(
                packet.get(Field.PROGRAM) or "",
                packet.get(Field.PROFILE) or "",
                port,
                packet.get(Field.ARGUMENTS) or [],
            )
        except (browser.BrowserError, OSError) as error:
            failed(str(error))
            return
        if forward is not None:
            error = await forward(port)
            if error is not None:
                browser.end(process, profile)
                failed(error)
                return
        self.held[launch] = (process, profile)
        self.client.send_packet(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: path, Field.REMOTE: kind}
        )
        try:
            await process.wait()
        finally:
            if self.held.pop(launch, None) is not None:
                self.client.send_packet({Field.CMD: Packet.BROWSER_ENDED, Field.BROWSER: launch})

    def stop(self, launch: str) -> None:
        "End a browser this client started, if it still runs."
        held = self.held.pop(launch, None)
        if held is not None:
            browser.end(*held)

    def stop_all(self) -> None:
        "End every browser this client started: the program that drove it cannot reach it any more."
        for launch in list(self.held):
            self.stop(launch)

    def reap(self) -> None:
        "Tell the server about each browser that ended by itself."
        for launch, (process, _profile) in list(self.held.items()):
            if process.poll() is not None:
                del self.held[launch]
                self.client.send_packet({Field.CMD: Packet.BROWSER_ENDED, Field.BROWSER: launch})

    def open_url(self, url: str) -> bool:
        """
        Ask this platform to open the URL in a browser, the way
        `webbrowser` picks: "open" on macOS, "xdg-open" or what $BROWSER
        names on Linux.

        True is a best effort: an opener can still fail after it
        started. A Linux machine without a display says False without
        trying, because the openers it does have say yes and fail
        anyway: `xdg-open` starts, finds no display and exits, and
        `webbrowser.open` counts the start as success. Knowing sooner
        is worth more than the chance of a `BROWSER` that names a
        browser which runs without a display.
        """
        if sys.platform != "darwin" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        try:
            return bool(webbrowser.open(url))
        except Exception:
            return False
