"""
Browsers that a program on the server drives, on the machine the person
sits at. Lillecarl/pyterm#655.

`pymux-browser` in a pane asks for one with `launch-browser`, which goes
to the client a person used last, asks there, and answers the port and
the path of the browser's endpoint once it listens. Then it holds the
browser with `hold-browser` for as long as it runs itself: that command
ends when the browser does, and stops the browser when its connection
closes, which is what killing `pymux-browser` does. `client/browser.py`
is the client's half.

A person who reaches this server through a pymux server of their own
(`connect ssh://...`) is no client here. Then the launch goes along that
peer link, and the peer asks them, starts the browser on its client and
forwards the port back on the link's SSH connection. Lillecarl/pyterm#1068.

**Every launch is asked.** A browser that a script drives can read what
its profile holds and go wherever it is sent, and the person whose
screen it opens on says yes to that, each time. As with `open-url -c`,
only a yes a person typed counts. Lillecarl/pyterm#445.
"""

from __future__ import annotations

import secrets
import shlex
import socket
from typing import TYPE_CHECKING

import anyio
from libpymux.protocol import Capability, Field, Message, Packet

if TYPE_CHECKING:
    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux
    from pymux.mirror.link import PeerLink

__all__ = ["ANSWER_SECONDS", "START_SECONDS", "BrowserRefused", "Browsers", "Launch"]

#: How long a question about a launch waits for the person.
ANSWER_SECONDS = 120.0

#: How long the client has to start the browser once the person said
#: yes: its own wait, `client.browser.START_SECONDS`, and a margin.
START_SECONDS = 30.0


class BrowserRefused(Exception):
    "The launch did not happen, and why, for the program in the pane to print."


class Launch:
    "One browser a program asked for, on a client here or through a peer link."

    def __init__(
        self,
        launch_id: str,
        arguments: list[str],
        pane_label: str,
        client_state: ClientState | None = None,
        link: PeerLink | None = None,
    ) -> None:
        self.launch_id = launch_id
        self.client_state = client_state
        self.link = link
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
        if self.client_state is None:
            return False
        return any(command == self.question_command for _text, command in self.client_state.confirmations)

    def send(self, packet: Message) -> None:
        "To the client or the peer that starts the browser."
        if self.link is not None:
            self.link.queue(packet)
        elif self.client_state is not None:
            self.client_state.client.connection.send_packet(packet)


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

    async def launch(self, arguments: list[str], pane_label: str, port: int = 0) -> Launch:
        """
        Ask the person used last, start the browser on their machine,
        and answer the launch once it listens. Raises `BrowserRefused`.

        `port` is the one the browser listens on, which a peer passes
        on from its owner; 0 picks a free one here.
        """
        client_state = self.pymux.latest_client()
        link = self._link_used_last(client_state)
        if link is not None:
            return await self._through(link, arguments, pane_label, port)
        if client_state is None:
            raise BrowserRefused("nobody is attached to open a browser for")
        client = client_state.client
        if not client.starts_browsers:
            raise BrowserRefused("the client in use cannot start a browser; update pymux there")

        launch = Launch(secrets.token_hex(8), arguments, pane_label, client_state=client_state)
        self.launches[launch.launch_id] = launch
        try:
            client_state.ask(self._question(launch), launch.question_command)
            self.pymux.invalidate()
            with anyio.move_on_after(ANSWER_SECONDS):
                while launch.asked() and not launch.approved:
                    await client_state.question_left()
            if not launch.approved:
                if launch.asked():
                    client_state.confirmations = [
                        one for one in client_state.confirmations if one[1] != launch.question_command
                    ]
                    raise BrowserRefused("nobody answered the question in %d seconds" % ANSWER_SECONDS)
                raise BrowserRefused("the person said no")

            launch.port = port or free_port()
            launch.send(
                {
                    Field.CMD: Packet.BROWSER,
                    Field.BROWSER: launch.launch_id,
                    Field.PORT: launch.port,
                    Field.PROGRAM: client_state.options.browser_program,
                    Field.PROFILE: client_state.options.browser_profile,
                    Field.ARGUMENTS: arguments,
                }
            )
            return await self._listening(launch, START_SECONDS, "the client")
        except BaseException:
            self.stop(launch.launch_id)
            raise

    def _link_used_last(self, client_state: ClientState | None) -> PeerLink | None:
        """
        The peer link to launch through: one that carries browsers, when
        a person there used this server after every client here that
        starts browsers.
        """
        links = [link for link in self.pymux.peer_links() if link.can(Capability.BROWSERS)]
        link = max(links, key=lambda one: one.last_used, default=None)
        if link is None:
            return None
        if (
            client_state is not None
            and client_state.client.starts_browsers
            and client_state.last_used >= link.last_used
        ):
            return None
        return link

    async def _through(self, link: PeerLink, arguments: list[str], pane_label: str, port: int) -> Launch:
        "Ask the peer, which asks its person: the question and the start both wait there."
        launch = Launch(secrets.token_hex(8), arguments, pane_label, link=link)
        self.launches[launch.launch_id] = launch
        try:
            # Free here, because the peer forwards the browser's port to
            # this machine under the same number.
            launch.port = port or free_port()
            launch.send(
                {
                    Field.CMD: Packet.BROWSER,
                    Field.BROWSER: launch.launch_id,
                    Field.PORT: launch.port,
                    Field.ARGUMENTS: arguments,
                    Field.NAME: pane_label,
                }
            )
            return await self._listening(
                launch, ANSWER_SECONDS + START_SECONDS, "the server on %s" % (link.host or "the peer",)
            )
        except BaseException:
            self.stop(launch.launch_id)
            raise

    async def _listening(self, launch: Launch, seconds: float, starter: str) -> Launch:
        with anyio.move_on_after(seconds):
            await launch.started.wait()
        if launch.error is not None:
            raise BrowserRefused(launch.error)
        if launch.path is None:
            raise BrowserRefused("%s did not start the browser in %d seconds" % (starter, seconds))
        return launch

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

    def started(self, packet: Message, link: PeerLink | None = None) -> None:
        "The client, or the peer of `link`, says the browser listens, or why it does not."
        launch = self.launches.get(packet.get(Field.BROWSER, ""))
        if launch is None or launch.link is not link:
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
        "Ask the client or the peer to end the browser, and forget it."
        launch = self.launches.get(launch_id)
        if launch is None:
            return
        if launch.port:
            launch.send({Field.CMD: Packet.BROWSER_STOP, Field.BROWSER: launch_id})
        self.ended(launch_id)

    def client_gone(self, client_state: ClientState) -> None:
        "A client that left took its browsers with it."
        for launch in list(self.launches.values()):
            if launch.client_state is client_state:
                self.ended(launch.launch_id)

    def link_gone(self, link: PeerLink) -> None:
        "A peer link that closed took its browsers with it: the peer ends them with the link."
        for launch in list(self.launches.values()):
            if launch.link is link:
                self.ended(launch.launch_id)
