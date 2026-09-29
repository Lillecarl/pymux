"""
One viewer watching one pane, over whatever carries the frames.

`protocol.py` decides what a frame holds; this holds a viewer's place in
a pane's life. It waits for the pane to say something changed, asks the
viewer's `PaneView` what that viewer has not seen, and sends it -- or
sends nothing, which is the answer most of the time.

**It carries frames and decides nothing.** The unix socket and the
websocket server are two callers of this, and neither adds a rule of its
own. `send` is a coroutine the caller gives, because a socket and a
websocket say "send" differently and neither belongs here.
Lillecarl/pymux#461.
"""

import json
from typing import Any, Awaitable, Callable, Dict, Optional

import anyio
from pyte.modes import PrivateMode

from pymux.commands import CommandException
from pymux.commands.common import send_key
from pymux.key_spelling import event_however_it_is_written
from pymux.log import logger
from pymux.web.protocol import PaneView, Typed, typed_of

__all__ = ["PaneStream"]

#: How long to wait before looking again when nothing said anything.
#:
#: The pane says when it changed, so this is not a poll: it is the floor
#: under a change that arrives some other way, and a resize by another
#: client is one -- a layout sizes a pane without the program in it
#: writing a byte. One second is slow enough to cost nothing and fast
#: enough that nobody watches a stale screen.
LOOK_AGAIN = 1.0


class PaneStream:
    """
    A pane, and the one viewer this stream serves.

    `writable` is the opener's choice and the server keeps it. Being
    straight about what it buys: a caller that holds pymux's socket can
    `send-keys` regardless, so this protects nothing against the opener.
    It protects against a bug in a relay's own loop turning a read-only
    viewer into a writing one. Defence in depth, not a boundary.
    """

    def __init__(
        self,
        pymux,
        pane,
        send: Callable[[Dict[str, Any]], Awaitable[None]],
        writable: bool = False,
    ) -> None:
        self.pymux = pymux
        self.pane = pane
        self.writable = writable
        self._send = send
        self._view = PaneView()
        self._changed = anyio.Event()
        self._closed = False

    # -- the loop ------------------------------------------------------

    async def run(self) -> None:
        """
        Serve this viewer until the pane ends or the caller stops.

        The handler goes on the terminal inside here, so a stream nobody
        runs hooks nothing and a stream that ends unhooks itself whatever
        ended it.
        """
        changed = self.pane.terminal.terminal_control.on_content_changed
        changed.add_handler(self._it_changed)
        try:
            await self._send(
                self._view.welcome(
                    self.pane.screen, self.pane.revision, self.writable
                )
            )
            await self._push()

            while not self._closed:
                with anyio.move_on_after(LOOK_AGAIN):
                    await self._changed.wait()
                self._changed = anyio.Event()

                # **A pane that ended ends the stream, and says nothing.**
                # The caller closes the connection, which is the whole of
                # how a client learns: a frame saying it as well would be
                # a path nothing can test, because the last pane of a
                # server takes the server down before anything can speak.
                if self.pane.process.is_terminated:
                    return

                await self._push()
        finally:
            changed.remove_handler(self._it_changed)

    async def _push(self) -> None:
        "Send what this viewer has not seen, if anything."
        frame = self._view.frame(self.pane.screen, self.pane.revision)
        if frame is not None:
            await self._send(frame)

    def _it_changed(self, _sender) -> None:
        self._changed.set()

    def close(self) -> None:
        "Stop after the frame in flight."
        self._closed = True
        self._changed.set()

    # -- what the viewer says ------------------------------------------

    def take(self, data: str) -> Optional[str]:
        """
        Read one message from the viewer, and answer why not if refused.

        It is synchronous because writing to a pty is: `write_input`
        hands bytes to the pane and returns. So a transport may call this
        from wherever it reads, without a task.
        """
        try:
            message = json.loads(data)
        except ValueError:
            return "that is not JSON"
        if not isinstance(message, dict):
            return "a message is an object"

        try:
            typed = typed_of(message)
        except ValueError as refused:
            return str(refused)

        if not self.writable:
            return "this stream does not take input"

        if self.pane.is_copying:
            # The same refusal `send-keys` gives. A person reading the
            # history of a pane is not typing into the program.
            return "the pane is in copy mode"

        try:
            self._type(typed)
        except CommandException as refused:
            return str(refused)
        return None

    def _type(self, typed: Typed) -> None:
        """
        Put what a viewer typed into the pane.

        A key arrives as its **name** and the pane spells it:
        `encode_key_event` reads the three keyboard modes and the pane is
        what knows which the program asked for, so one message means the
        right bytes in a pane running `vim` and in one that asked for the
        kitty protocol.

        Text a viewer composed goes as it stands and is **not** bracketed:
        a program that asked to know about pastes would draw the markers
        around a character somebody typed.
        """
        if not typed.named:
            text = typed.text
            if typed.bracketed:
                text = _a_paste(self.pane, text)
            self.pane.process.write_input(text)
            return

        for name in typed.text.split():
            try:
                event = event_however_it_is_written(name)
            except ValueError:
                # Not a key anybody names. `send-keys` sends such a word
                # as text, and so does this.
                self.pane.process.write_input(name)
                continue
            send_key(self.pane, event, name)


def _a_paste(pane, text: str) -> str:
    """
    A paste, marked as one when the program asked for that.

    A program that set private mode 2004 wants to know where a paste
    starts and ends, so that it does not read what somebody pasted as
    the keys they pressed -- which is how a paste into a shell runs the
    lines in it. A program that did not ask reads plain text, and
    wrapping it anyway would put the two markers on its screen.
    """
    if PrivateMode.BRACKETED_PASTE.flag not in pane.screen.mode:
        return text
    return "\x1b[200~%s\x1b[201~" % (text,)
