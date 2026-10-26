"""
Browsers that a program on the server drives, on the machine the person
sits at. Lillecarl/pymux#653.

`pymux-browser` in a pane asks for one with `launch-browser`, which goes
to the client a person used last, asks there, and answers the port and
the path of the browser's endpoint once it listens. Then it holds the
browser with `hold-browser` for as long as it runs itself: that command
ends when the browser does, and stops the browser when its connection
closes, which is what killing `pymux-browser` does. `client/browser.py`
is the client's half.

**Every launch is asked.** A browser that a script drives can read what
its profile holds and go wherever it is sent, and the person whose
screen it opens on says yes to that, each time. As with `open-url -c`,
only a yes a person typed counts. Lillecarl/pymux#443.
"""

from __future__ import annotations

import secrets
import shlex
import socket
from typing import TYPE_CHECKING

import anyio
from libpymux.protocol import Field, Packet

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux

__all__ = ["ANSWER_SECONDS", "START_SECONDS", "BrowserRefused", "Browsers", "Launch"]

#: How long a question about a launch waits for the person.
ANSWER_SECONDS = 120.0

#: How long the client has to start the browser once the person said
#: yes: its own wait, `client.browser.START_SECONDS`, and a margin.
START_SECONDS = 30.0

#: How often a waiting launch looks whether its question was answered.
QUESTION_POLL = 0.1


class BrowserRefused(Exception):
    "The launch did not happen, and why, for the program in the pane to print."


class Launch:
    "One browser a program asked for."

    def __init__(self, launch_id: str, client_state: ClientState, arguments: list[str], pane_label: str) -> None:
        self.launch_id = launch_id
        self.client_state = client_state
        self.arguments = arguments
        self.pane_label = pane_label
        self.port = 0
        self.approved = False
        self.path: str | None = None
        #: "cdp" for a Chromium, "bidi" for a Firefox.
        self.remote = "cdp"
        self.error: str | None = None
        self.started = anyio.Event()
        self.ended = anyio.Event()

    @property
    def question_command(self) -> str:
        return "launch-browser -c %s" % (self.launch_id,)

    def asked(self) -> bool:
        "Whether its question still waits on the client."
        return any(command == self.question_command for _text, command in self.client_state.confirmations)


def free_port() -> int:
    "A port nothing on this machine listens on, now."
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Browsers:
    "The launches of one server, by id."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        self.launches: dict[str, Launch] = {}

    async def launch(self, arguments: list[str], pane_label: str) -> Launch:
        """
        Ask the client a person used last, start the browser there, and
        answer the launch once it listens. Raises `BrowserRefused`.
        """
        client_state = self.pymux.latest_client()
        if client_state is None:
            raise BrowserRefused("nobody is attached to open a browser for")
        connection = client_state.connection
        if not connection.starts_browsers:
            raise BrowserRefused("the client in use cannot start a browser; update pymux there")

        launch = Launch(secrets.token_hex(8), client_state, arguments, pane_label)
        self.launches[launch.launch_id] = launch
        try:
            client_state.ask(self._question(launch), launch.question_command)
            self.pymux.invalidate()
            with anyio.move_on_after(ANSWER_SECONDS):
                while launch.asked() and not launch.approved:
                    await anyio.sleep(QUESTION_POLL)
            if not launch.approved:
                if launch.asked():
                    client_state.confirmations = [
                        one for one in client_state.confirmations if one[1] != launch.question_command
                    ]
                    raise BrowserRefused("nobody answered the question in %d seconds" % ANSWER_SECONDS)
                raise BrowserRefused("the person said no")

            launch.port = free_port()
            connection._send_packet(
                {
                    Field.CMD: Packet.BROWSER,
                    Field.BROWSER: launch.launch_id,
                    Field.PORT: launch.port,
                    Field.PROGRAM: client_state.options.browser_program,
                    Field.PROFILE: client_state.options.browser_profile,
                    Field.ARGUMENTS: arguments,
                }
            )
            with anyio.move_on_after(START_SECONDS):
                await launch.started.wait()
            if launch.error is not None:
                raise BrowserRefused(launch.error)
            if launch.path is None:
                raise BrowserRefused("the client did not start the browser in %d seconds" % START_SECONDS)
            return launch
        except BaseException:
            self.stop(launch.launch_id)
            raise

    def _question(self, launch: Launch) -> str:
        # What it opens, and none of the flags: Playwright passes forty,
        # and a question of one screen line showed nothing else.
        shown = " ".join(shlex.quote(one) for one in launch.arguments if not one.startswith("-"))
        return "%s wants to drive a browser on this machine%s. Open it? (y/n)" % (
            launch.pane_label,
            " (%s)" % shown if shown else "",
        )

    def approve(self, launch_id: str) -> None:
        "The person said yes."
        launch = self.launches.get(launch_id)
        if launch is not None:
            launch.approved = True

    def started(self, packet: dict) -> None:
        "The client says the browser listens, or why it does not."
        launch = self.launches.get(packet.get(Field.BROWSER, ""))
        if launch is None:
            return
        launch.path = packet.get(Field.PATH)
        launch.remote = packet.get(Field.REMOTE) or "cdp"
        launch.error = packet.get(Field.ERROR)
        launch.started.set()
        if launch.error is not None:
            launch.ended.set()

    def ended(self, launch_id: str) -> None:
        "The browser is gone, by itself or because it was stopped."
        launch = self.launches.pop(launch_id, None)
        if launch is not None:
            launch.started.set()
            launch.ended.set()

    def stop(self, launch_id: str) -> None:
        "Ask the client to end the browser, and forget it."
        launch = self.launches.get(launch_id)
        if launch is None:
            return
        connection = launch.client_state.connection
        if connection is not None and launch.port:
            connection._send_packet({Field.CMD: Packet.BROWSER_STOP, Field.BROWSER: launch_id})
        self.ended(launch_id)

    def client_gone(self, client_state: ClientState) -> None:
        "A client that left took its browsers with it."
        for launch in list(self.launches.values()):
            if launch.client_state is client_state:
                self.ended(launch.launch_id)
