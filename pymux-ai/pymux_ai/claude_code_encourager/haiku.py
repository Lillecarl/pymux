"""
One pane's conversation with Claude Haiku, through aid's agent loop
(`aid_agent.loop.run`).

The conversation grows by one stop and one answer each time the agent
stops. The instructions lead it and never change, and the provider
caches the longest prefix it saw, so a stop costs its own tokens and a
cache read of the rest. Pictures go inline, so they sit in that prefix
too. Past `COMPACT_AT` input tokens, Haiku sums the stops up and the
conversation starts again from that summary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aid_agent.attachments import AttachmentStore
from aid_agent.loop import run
from aid_agent.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
from aid_agent.model import model_for

from .verdict import INSTRUCTIONS, Verdict, describe

if TYPE_CHECKING:
    from aid_agent.loop import Turn
    from aid_agent.messages import ModelMessage, Part
    from aid_agent.model import Model

    from ..observe import Observation

__all__ = ["COMPACT_AT", "MODEL", "HaikuConversation"]

MODEL: Final = "anthropic/claude-haiku-5-5"

#: Haiku 5.5's context window, as models.dev gave it on 2026-11-18.
WINDOW: Final = 1_000_000

#: Half the window. A stop with its picture is about 3 000 tokens.
COMPACT_AT: Final = WINDOW // 2

#: A yes or no about a screen needs little thought, and thought is billed.
SETTINGS: Final = {"output_config": {"effort": "low"}}

SUMMARISE: Final = """\
Sum up this conversation for the conversation that replaces it: what
the agent works on, how far it got, each time it stopped and why, what
you answered, and any pattern a later answer should weigh, such as an
agent that keeps stopping at the same point. Plain text, at most 300
words."""


def _store_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "pymux" / "encourager"


class HaikuConversation:
    def __init__(self, model: Model | None = None, store: AttachmentStore | None = None) -> None:
        self._model = model if model is not None else model_for(MODEL, SETTINGS)
        self._store = store if store is not None else AttachmentStore(_store_root())
        self.history: list[ModelMessage] = []
        self.last_input = 0

    async def judge(self, observation: Observation) -> Verdict:
        if self.last_input > COMPACT_AT:
            await self.compact()
        picture = await self._store.add(observation.png)
        turn = await self._ask([UserPromptPart(describe(observation)), picture], Verdict)
        self.history += turn.messages
        usage = turn.usage
        self.last_input = (usage.input_tokens or 0) + (usage.cache_read_tokens or 0) + (usage.cache_write_tokens or 0)
        return turn.output

    async def compact(self) -> None:
        turn = await self._ask([UserPromptPart(SUMMARISE)], str)
        self.history = [
            ModelRequest(
                parts=[
                    SystemPromptPart(INSTRUCTIONS),
                    UserPromptPart("What happened in this pane before, summed up:\n\n" + turn.output),
                ]
            ),
            ModelResponse(parts=[TextPart("Understood.")]),
        ]
        self.last_input = 0

    async def _ask(self, prompt: list[Part], output: Any) -> Turn:
        return await run(
            self._model,
            self.history,
            prompt,
            instructions=INSTRUCTIONS,
            settings=SETTINGS,
            output_type=output,
            attachments=self._store,
            inline=True,
        )
