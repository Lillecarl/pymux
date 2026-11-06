"""
Firefox's Remote Debugging Protocol, the one its DevTools speak, over a
Unix socket: the protocol Firefox and Glide serve with
`--start-debugger-server <path>`.

Measured on Glide 0.1.65a, Gecko 159:

- A packet is `<length>:<json>`, the length in bytes.
- The root actor greets first, unasked.
- An actor answers its requests in order. A packet that carries `type`
  is an event, not an answer: an actor sends events between its
  answers, and the answers carry no `type`.
- `evaluateJSAsync` answers a `resultID` at once, and the value later,
  in an `evaluationResult` event with that id. With `mapped.await` it
  awaits a promise; one that rejects answers `topLevelAwaitRejected`
  and no reason, so code sent here catches its own errors.
- A string longer than the protocol sends whole arrives as a
  `longString` grip, and its `substring` request answers the rest.
- An actor's name holds only for the connection that made it.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import anyio
from anyio.abc import SocketStream

__all__ = ["Connection", "RDPError", "connected"]

type JsonObject = dict[str, Any]
type Listener = Callable[[JsonObject], None]

TIMEOUT: Final = 30.0


class RDPError(RuntimeError):
    "An error the browser answered, or a connection that went away."


@dataclass
class _Pending:
    done: anyio.Event = field(default_factory=anyio.Event)
    reply: JsonObject | None = None
    error: RDPError | None = None


class Connection:
    "One connection to a browser's debugger server. `connected` opens it."

    def __init__(self, stream: SocketStream) -> None:
        self._stream = stream
        self._pending: dict[str, deque[_Pending]] = {}
        self._listeners: dict[tuple[str, str], list[Listener]] = {}
        self._buffer = b""
        self._closed: RDPError | None = None
        self.greeting: anyio.Event = anyio.Event()

    async def send(self, to: str, kind: str, timeout: float = TIMEOUT, **params: Any) -> JsonObject:
        "Send request `kind` to the actor `to`, and answer its reply."
        if self._closed is not None:
            raise self._closed
        pending = _Pending()
        self._pending.setdefault(to, deque()).append(pending)
        data = json.dumps({"to": to, "type": kind, **params}).encode()
        try:
            await self._stream.send(b"%d:%s" % (len(data), data))
            with anyio.fail_after(timeout):
                await pending.done.wait()
        except TimeoutError:
            # The request keeps its place in the queue, so the actor's late
            # answer lands on it and not on the next request.
            raise RDPError("%s to %s: no answer in %gs" % (kind, to, timeout)) from None
        if pending.error is not None:
            raise pending.error
        return pending.reply or {}

    def listen(self, actor: str, kind: str, listener: Listener) -> Callable[[], None]:
        "Call `listener` with each event `kind` from `actor`; answers what stops it."
        listeners = self._listeners.setdefault((actor, kind), [])
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    async def read(self) -> None:
        try:
            async for chunk in self._stream:
                self._buffer += chunk
                while (packet := self._take()) is not None:
                    self._dispatch(packet)
        except anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError:
            pass
        finally:
            self._closed = RDPError("the browser closed the connection")
            for queue in self._pending.values():
                for pending in queue:
                    pending.error = self._closed
                    pending.done.set()

    def _take(self) -> JsonObject | None:
        size, colon, rest = self._buffer.partition(b":")
        if not colon:
            return None
        length = int(size)
        if len(rest) < length:
            return None
        self._buffer = rest[length:]
        return json.loads(rest[:length])

    def _dispatch(self, packet: JsonObject) -> None:
        actor = packet.get("from", "")
        if actor == "root" and "applicationType" in packet:
            self.greeting.set()
            return
        if "type" in packet and "error" not in packet:
            for listener in list(self._listeners.get((actor, packet["type"]), ())):
                listener(packet)
            return
        queue = self._pending.get(actor)
        if not queue:
            return
        pending = queue.popleft()
        if "error" in packet:
            pending.error = RDPError("%s: %s" % (packet["error"], packet.get("message", "")))
        else:
            pending.reply = packet
        pending.done.set()

    async def text(self, grip: Any) -> Any:
        "A value as `evaluationResult` gave it, with a long string read whole."
        if isinstance(grip, dict) and grip.get("type") == "longString":
            answer = await self.send(grip["actor"], "substring", start=0, end=grip["length"])
            return answer["substring"]
        return grip

    async def evaluate(self, console: str, text: str, timeout: float = TIMEOUT) -> Any:
        """
        Run `text` in the console actor `console`, and answer its value as
        the protocol gives it: a JSON value, a grip for anything else.
        A promise is awaited. Raises with the message of what it threw.
        """
        results: dict[str, JsonObject] = {}
        arrived = anyio.Event()

        def result(packet: JsonObject) -> None:
            results[packet["resultID"]] = packet
            arrived.set()

        stop = self.listen(console, "evaluationResult", result)
        try:
            started = await self.send(console, "evaluateJSAsync", text=text, mapped={"await": True})
            wanted = started["resultID"]
            with anyio.fail_after(timeout):
                while wanted not in results:
                    await arrived.wait()
                    arrived = anyio.Event()
        except TimeoutError:
            raise RDPError("no value in %gs" % (timeout,)) from None
        finally:
            stop()
        found = results[wanted]
        if found.get("topLevelAwaitRejected"):
            raise RDPError("the promise rejected; the console does not say why")
        if found.get("hasException"):
            raise RDPError(await self.text(found.get("exceptionMessage")) or "it threw")
        return await self.text(found.get("result"))


@asynccontextmanager
async def connected(path: Path) -> AsyncIterator[Connection]:
    "A connection to the debugger server at the socket `path`, open for the block."
    try:
        stream = await anyio.connect_unix(path)
    except OSError as error:
        raise RDPError("no debugger server at %s: %s" % (path, error.strerror or error)) from None
    try:
        async with stream, anyio.create_task_group() as tasks:
            connection = Connection(stream)
            tasks.start_soon(connection.read)
            try:
                with anyio.fail_after(TIMEOUT):
                    await connection.greeting.wait()
                yield connection
            finally:
                tasks.cancel_scope.cancel()
    except BaseExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise
