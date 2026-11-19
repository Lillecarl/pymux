"""
The plugin. Each pane has a conversation of its own with the model, so
it sees the agent's earlier stops and its own earlier answers.

The command line imports this module for its commands too, so the
picture code and the model client import only when a pane is looked at.
"""

from __future__ import annotations

import functools
import random
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Protocol

import anyio
from libpymux import Pane
from pyte.program_status import BlockedKind, ProgramState

from pymux.plugin_api import PaneEnded, Plugin, ProgramReported

from .verdict import Decision, Verdict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pymux.ids import PaneId
    from pymux.plugin_api import Host

    from ..observe import Observation

__all__ = ["APP", "Conversation", "Encourager", "keys_to_free_text"]

#: The `app` Claude Code reports under.
APP: Final = "claude-code"

#: How long a `done` must stand before the pane is looked at: an agent
#: that goes on by itself, or a person who types, says `working` first.
SETTLE: Final = 5.0

#: How long a `done` waits while the agent has background work open: the
#: work's end wakes the agent, which says `working` and so drops the look.
#: Wrapty's Stop hook holds the same way.
BACKGROUND_WAIT: Final = 600.0

#: How long a question stands before the model is asked whether the
#: agent may answer it itself; again while a person has the pane in
#: front, up to the longest.
QUESTION_WAIT: Final = 300.0
QUESTION_LONGEST: Final = 1800.0

#: What answers a question the model let the agent settle. It names no
#: option: the encourager knows too little to pick one.
DEFER: Final = (
    "If you have done your research and found no better alternative, take the option you recommended"
    " and say why in one line. If you cannot say that honestly, stop and wait for the person."
)

#: The last option of a Claude Code question: a field for free text.
FREE_TEXT: Final = re.compile(r"^\W*(\d+)\.\s+Type something", re.MULTILINE)

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
        if self._conversation is None:
            from .haiku import HaikuConversation, key_path, read_key

            key = await read_key()
            if key is None:
                self.host.log.warning("no Anthropic key at %s, so the encourager stays idle", key_path())
                return
            self._conversation = functools.partial(HaikuConversation, key)
        # A question put before this start, as before an upgrade, was
        # never looked at. A `done` was, by the plugin before this one.
        for pane_id, report in self.host.program_reports().items():
            if report.state is ProgramState.BLOCKED:
                await self.on_program_reported(ProgramReported(pane_id, report))

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
        elif report.state is ProgramState.BLOCKED and report.kind is BlockedKind.QUESTION:
            self.host.start_soon(self._consider_question, event, watch, watch.generation)

    async def on_pane_ended(self, event: PaneEnded) -> None:
        self._watches.pop(event.pane_id, None)

    async def _consider(self, event: ProgramReported, watch: _Watch, generation: int) -> None:
        "Never raises: an exception in a task of the plugin's fails the whole plugin."
        try:
            await anyio.sleep(SETTLE)
            if watch.generation != generation:
                return
            from .transcript import background_of

            waiting = await background_of(self.host, event.pane_id)
            if waiting:
                self.host.log.info("%%%d: waits on %s, so the look waits", event.pane_id, ", ".join(map(str, waiting)))
                await anyio.sleep(BACKGROUND_WAIT)
                if watch.generation != generation:
                    return
            async with watch.lock:
                await self._look(event, watch, generation)
        except Exception:
            self.host.log.exception("pane %%%d: the look failed", event.pane_id)

    async def _consider_question(self, event: ProgramReported, watch: _Watch, generation: int) -> None:
        "Never raises, as `_consider`."
        try:
            await anyio.sleep(QUESTION_WAIT)
            waited = QUESTION_WAIT
            # A plugin hears no event when a person looks at a pane, so
            # whether one does is read again after each wait.
            while watch.generation == generation and waited < QUESTION_LONGEST and self.host.in_front(event.pane_id):
                await anyio.sleep(QUESTION_WAIT)
                waited += QUESTION_WAIT
            if watch.generation != generation:
                return
            async with watch.lock:
                await self._look(event, watch, generation)
        except Exception:
            self.host.log.exception("pane %%%d: the look at a question failed", event.pane_id)

    async def _look(self, event: ProgramReported, watch: _Watch, generation: int) -> None:
        pane = "%%%d" % event.pane_id
        now = anyio.current_time()
        if watch.nudges and now - watch.last_nudge > QUIET_RESETS:
            watch.nudges = 0
        if watch.nudges >= MOST_NUDGES:
            self.host.log.info("%s: %d nudges in a row, so it is left alone", pane, watch.nudges)
            return
        from ..observe import observe
        from .transcript import background_of

        observation = await observe(self.host, event.pane_id, event.report)
        waiting = await background_of(self.host, event.pane_id)
        observation = replace(observation, waiting_on=tuple(map(str, waiting)))
        verdict = await watch.conversation.judge(observation)
        self.host.log.info("%s: %s, %s", pane, verdict.decision, verdict.reason)
        question = event.report.state is ProgramState.BLOCKED
        if verdict.decision is Decision.STOP or not (question or verdict.message.strip()):
            watch.nudges = 0
            return
        if watch.generation != generation:
            self.host.log.info("%s: the agent moved while the model looked, so nothing is typed", pane)
            return
        if question:
            keys = keys_to_free_text(observation.text)
            if keys is None:
                self.host.log.info("%s: the question shows no field to type in, so nothing is typed", pane)
                return
            await self._type(pane, SIGNATURE + DEFER, keys)
        else:
            await self._type(pane, SIGNATURE + verdict.message.strip())
        watch.nudges += 1
        watch.last_nudge = now
        self.host.pymux.show_message("encourager, %s: %s" % (pane, verdict.reason))

    async def _type(self, pane: str, text: str, keys: Sequence[str] = ()) -> None:
        "Press `keys`, then type `text` and its Return."
        target = Pane(self.host.server, {"pane_id": pane})
        for key in keys:
            await target.send_key(key)
        await target.send_keys(text, enter=False)
        await anyio.sleep(random.uniform(*NO_PASTE_DELAY))
        await target.send_key("Enter")


def keys_to_free_text(screen: str) -> list[str] | None:
    """
    The keys that move a question's choice from its first option to its
    field for free text, or None when the screen shows no such field.
    The last one on the screen is the question still open.
    """
    found = FREE_TEXT.findall(screen)
    if not found:
        return None
    return ["Down"] * (int(found[-1]) - 1)
