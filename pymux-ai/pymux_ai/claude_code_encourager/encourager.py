"""
The plugin. Each pane has a conversation of its own with the model, so
it sees the agent's earlier stops and its own earlier answers.

The command line imports this module for its commands too, so the
picture code and the model client import only when a pane is looked at.
"""

from __future__ import annotations

import functools
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol

import anyio
from libpymux import Pane
from pyte.program_status import ProgramState

from pymux.plugin_api import PaneEnded, Plugin, ProgramReported

from .verdict import Decision, Verdict

if TYPE_CHECKING:
    from collections.abc import Callable

    from pymux.ids import PaneId
    from pymux.plugin_api import Host

    from ..observe import Observation

__all__ = ["APP", "Conversation", "Encourager"]

#: The `app` Claude Code reports under.
APP: Final = "claude-code"

#: How long a `done` must stand before the pane is looked at: an agent
#: that goes on by itself, or a person who types, says `working` first.
SETTLE: Final = 5.0

#: Nudges in a row before the encourager leaves a pane alone, whatever
#: the model says, until a `stop` or an hour without a nudge.
MOST_NUDGES: Final = 5
QUIET_RESETS: Final = 3600.0

#: Text and its Return apart: an agent's prompt reads a Return inside
#: the paste burst as a newline, and the line goes unsent. As pymux-mcp.
NO_PASTE_DELAY: Final = (0.25, 0.5)

#: What starts a typed nudge, so the agent and the person see who wrote it.
SIGNATURE: Final = "[encourager] "


class Conversation(Protocol):
    "One pane's conversation with the model."

    async def judge(self, observation: Observation) -> Verdict: ...


@dataclass(eq=False)
class _Watch:
    conversation: Conversation
    #: Moves on every report, so a look that a newer report overtook is
    #: thrown away.
    generation: int = 0
    nudges: int = 0
    last_nudge: float = 0.0
    lock: anyio.Lock = field(default_factory=anyio.Lock)


class Encourager(Plugin):
    def __init__(self, host: Host, conversation: Callable[[], Conversation] | None = None) -> None:
        super().__init__(host)
        #: None until `run` found the key: until then, and without one,
        #: nothing is looked at.
        self._conversation = conversation
        self._watches: dict[PaneId, _Watch] = {}

    async def run(self) -> None:
        if self._conversation is not None:
            return
        from .haiku import HaikuConversation, key_path, read_key

        key = await read_key()
        if key is None:
            self.host.log.warning("no Anthropic key at %s, so the encourager stays idle", key_path())
            return
        self._conversation = functools.partial(HaikuConversation, key)

    async def on_program_reported(self, event: ProgramReported) -> None:
        report = event.report
        if report.app != APP or report.id or self._conversation is None:
            return
        watch = self._watches.get(event.pane_id)
        if watch is None:
            watch = self._watches[event.pane_id] = _Watch(self._conversation())
        watch.generation += 1
        if report.state is ProgramState.DONE:
            self.host.start_soon(self._consider, event, watch, watch.generation)

    async def on_pane_ended(self, event: PaneEnded) -> None:
        self._watches.pop(event.pane_id, None)

    async def _consider(self, event: ProgramReported, watch: _Watch, generation: int) -> None:
        "Never raises: an exception in a task of the plugin's fails the whole plugin."
        try:
            await anyio.sleep(SETTLE)
            if watch.generation != generation:
                return
            async with watch.lock:
                await self._look(event, watch, generation)
        except Exception:
            self.host.log.exception("pane %%%d: the look failed", event.pane_id)

    async def _look(self, event: ProgramReported, watch: _Watch, generation: int) -> None:
        pane = "%%%d" % event.pane_id
        now = anyio.current_time()
        if watch.nudges and now - watch.last_nudge > QUIET_RESETS:
            watch.nudges = 0
        if watch.nudges >= MOST_NUDGES:
            self.host.log.info("%s: %d nudges in a row, so it is left alone", pane, watch.nudges)
            return
        from ..observe import observe

        verdict = await watch.conversation.judge(await observe(self.host, event.pane_id, event.report))
        self.host.log.info("%s: %s, %s", pane, verdict.decision, verdict.reason)
        if verdict.decision is Decision.STOP or not verdict.message.strip():
            watch.nudges = 0
            return
        if watch.generation != generation:
            self.host.log.info("%s: the agent moved while the model looked, so nothing is typed", pane)
            return
        await self._type(pane, SIGNATURE + verdict.message.strip())
        watch.nudges += 1
        watch.last_nudge = now
        self.host.pymux.show_message("encourager, %s: %s" % (pane, verdict.reason))

    async def _type(self, pane: str, text: str) -> None:
        target = Pane(self.host.server, {"pane_id": pane})
        await target.send_keys(text, enter=False)
        await anyio.sleep(random.uniform(*NO_PASTE_DELAY))
        await target.send_key("Enter")
