"""
One pane's conversation with Claude Haiku, through aid's model
transports (`aid_agent`).

The conversation grows by one stop and one answer each time the agent
stops. The instructions lead it and never change, and the provider
caches the longest prefix it saw, so a stop costs its own tokens and a
cache read of the rest. Past `COMPACT_AT` input tokens, Haiku sums the
stops up and the conversation starts again from that summary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from aid_agent.attachments import AttachmentStore
from aid_agent.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
from aid_agent.model import AnthropicModel, ModelInfo, RequestUsage, accumulate

from .verdict import INSTRUCTIONS, Verdict, describe

if TYPE_CHECKING:
    from aid_agent.messages import ModelMessage
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
        self._model = model if model is not None else AnthropicModel(MODEL)
        self._store = store if store is not None else AttachmentStore(_store_root())
        self.history: list[ModelMessage] = []
        self.last_input = 0

    async def judge(self, observation: Observation) -> Verdict:
        if self.last_input > COMPACT_AT:
            await self.compact()
        picture = await self._store.add(observation.png)
        parts = [UserPromptPart(describe(observation)), picture]
        request = ModelRequest(parts=[SystemPromptPart(INSTRUCTIONS), *parts] if not self.history else parts)
        response, usage = await self._ask([*self.history, request], Verdict)
        self.history += [request, response]
        self.last_input = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        return Verdict.model_validate_json(_text(response))

    async def compact(self) -> None:
        response, _ = await self._ask([*self.history, ModelRequest(parts=[UserPromptPart(SUMMARISE)])], str)
        self.history = [
            ModelRequest(
                parts=[
                    SystemPromptPart(INSTRUCTIONS),
                    UserPromptPart("What happened in this pane before, summed up:\n\n" + _text(response)),
                ]
            ),
            ModelResponse(parts=[TextPart("Understood.")]),
        ]
        self.last_input = 0

    async def _ask(self, messages: list[ModelMessage], output: type) -> tuple[ModelResponse, RequestUsage]:
        "One request. Usage is the last the stream reported: Anthropic's message_delta carries the totals."
        info = ModelInfo(output_type=output, settings=SETTINGS, attachments=self._store)
        response = ModelResponse(parts=[], model_name=self._model.model_name)
        indexes: dict[int, int] = {}
        usage = RequestUsage()
        async for delta in self._model.stream(messages, info):
            accumulate(response, indexes, delta)
            if delta.usage is not None:
                usage = _merged(usage, delta.usage)
        return response, usage


def _merged(seen: RequestUsage, new: RequestUsage) -> RequestUsage:
    "message_start carries the input counts and message_delta the output, each with zeros for the other."
    return RequestUsage(
        input_tokens=new.input_tokens or seen.input_tokens,
        output_tokens=new.output_tokens or seen.output_tokens,
        cache_read_tokens=new.cache_read_tokens or seen.cache_read_tokens,
        cache_write_tokens=new.cache_write_tokens or seen.cache_write_tokens,
    )


def _text(response: ModelResponse) -> str:
    return "".join(part.content for part in response.parts if isinstance(part, TextPart))
