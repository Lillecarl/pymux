"""
The Claude Code encourager, with a model that answers from a list and a
host that records what the plugin types.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
import pytest
from aid_agent.attachments import AttachmentStore
from aid_agent.messages import AttachmentPart, SystemPromptPart, TextPart, UserPromptPart
from aid_agent.model import RequestUsage, StreamDelta
from pymux_ai.claude_code_encourager import encourager as module
from pymux_ai.claude_code_encourager.encourager import Encourager
from pymux_ai.claude_code_encourager.haiku import COMPACT_AT, HaikuConversation
from pymux_ai.claude_code_encourager.verdict import INSTRUCTIONS, Decision, Verdict
from pymux_ai.observe import Observation
from pyte.program_status import parse_report

from pymux.ids import PaneId
from pymux.plugin_api import ProgramReported

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from aid_agent.messages import ModelMessage
    from aid_agent.model import ModelInfo

#: The smallest PNG: what `AttachmentStore.put` checks is the signature.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)

DONE = parse_report("state=done:app=claude-code")


@dataclass
class ListModel:
    "Answers each request with the next text, and keeps what it was sent."

    answers: list[str]
    input_tokens: int = 1000
    model_name: str = "anthropic/claude-haiku-5-5"
    asked: list[list[ModelMessage]] = field(default_factory=list)

    async def stream(self, messages: list[ModelMessage], info: ModelInfo) -> AsyncGenerator[StreamDelta]:
        self.asked.append([message.model_copy(deep=True) for message in messages])
        yield StreamDelta(usage=RequestUsage(input_tokens=self.input_tokens))
        yield StreamDelta(0, TextPart(self.answers.pop(0)))
        yield StreamDelta(usage=RequestUsage(output_tokens=20))


def _verdict(decision: Decision, message: str = "") -> str:
    return Verdict(decision=decision, reason="because", message=message).model_dump_json()


def _observation(text: str = "> done") -> Observation:
    return Observation("%1", DONE, "/work", text, PNG, ())


async def test_the_instructions_lead_and_the_prefix_never_changes(tmp_path):
    "Each request starts with the one before it, so the provider's cache covers it."
    model = ListModel([_verdict(Decision.CONTINUE, "Go on with the tests."), _verdict(Decision.STOP)])
    conversation = HaikuConversation("key", model, AttachmentStore(tmp_path))

    first = await conversation.judge(_observation("first"))
    second = await conversation.judge(_observation("second"))

    assert (first.decision, first.message, second.decision) == (Decision.CONTINUE, "Go on with the tests.", "stop")
    one, two = model.asked
    assert one[0].parts[0] == SystemPromptPart(INSTRUCTIONS)
    assert [type(part) for part in one[-1].parts[-2:]] == [UserPromptPart, AttachmentPart]
    assert two[: len(one)] == one
    assert not any(isinstance(part, SystemPromptPart) for message in two[len(one) :] for part in message.parts)


async def test_past_half_the_window_the_conversation_starts_from_a_summary(tmp_path):
    model = ListModel([_verdict(Decision.STOP), "It kept stopping after the build.", _verdict(Decision.STOP)])
    model.input_tokens = COMPACT_AT + 1
    conversation = HaikuConversation("key", model, AttachmentStore(tmp_path))

    await conversation.judge(_observation())
    model.input_tokens = 1000
    await conversation.judge(_observation())

    summary, after = model.asked[1:]
    assert "Sum up this conversation" in str(summary[-1].parts[0].content)
    assert after[0].parts[0] == SystemPromptPart(INSTRUCTIONS)
    assert "It kept stopping after the build." in str(after[0].parts[1].content)
    assert len(conversation.history) == 4
    assert conversation.last_input == 1000


@dataclass
class ListConversation:
    verdicts: list[Verdict]
    seen: list[Observation] = field(default_factory=list)

    async def judge(self, observation: Observation) -> Verdict:
        self.seen.append(observation)
        return self.verdicts.pop(0)


class Host:
    "What the plugin reaches: a task group, a log and a message line."

    def __init__(self, tasks: anyio.abc.TaskGroup) -> None:
        self._tasks = tasks
        self.log = logging.getLogger("test-encourager")
        self.messages: list[str] = []
        self.pymux = self

    def start_soon(self, func, *args) -> None:
        self._tasks.start_soon(func, *args)

    def show_message(self, text: str) -> None:
        self.messages.append(text)


@pytest.fixture
def quick(monkeypatch):
    "No settling, and what is typed recorded, with no server."
    monkeypatch.setattr(module, "SETTLE", 0.01)
    typed: list[tuple[str, str]] = []

    async def observe(host, pane_id, report):
        return _observation()

    async def type_(self, pane, text):
        typed.append((pane, text))

    monkeypatch.setattr("pymux_ai.observe.observe", observe)
    monkeypatch.setattr(Encourager, "_type", type_)
    return typed


async def _heard(conversation: ListConversation, *reports: str) -> Host:
    async with anyio.create_task_group() as tasks:
        host = Host(tasks)
        plugin = Encourager(host, lambda: conversation)  # type: ignore[arg-type]
        for report in reports:
            await plugin.on_program_reported(ProgramReported(PaneId(1), parse_report(report)))
    return host


async def test_a_done_agent_that_could_go_on_is_told_to(quick):
    conversation = ListConversation([Verdict(decision=Decision.CONTINUE, reason="next step named", message="Go on.")])
    host = await _heard(conversation, "state=done:app=claude-code")
    assert quick == [("%1", "[encourager] Go on.")]
    assert host.messages == ["encourager, %1: next step named"]


async def test_a_stop_types_nothing(quick):
    await _heard(
        ListConversation([Verdict(decision=Decision.STOP, reason="it asked a question")]), "state=done:app=claude-code"
    )
    assert quick == []


async def test_a_done_overtaken_before_it_settles_is_never_looked_at(quick):
    conversation = ListConversation([])
    await _heard(conversation, "state=done:app=claude-code", "state=working:app=claude-code")
    assert (conversation.seen, quick) == ([], [])


async def test_without_a_key_nothing_is_looked_at(quick, tmp_path, monkeypatch):
    "A missing key is said once, at the start, and not at every stop."
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks))
        await plugin.run()
        await plugin.on_program_reported(ProgramReported(PaneId(1), DONE))
    assert quick == []
    assert plugin._watches == {}


async def test_the_key_comes_from_its_file_and_never_the_environment(tmp_path, monkeypatch):
    "Every pane inherits the server's environment, and a Claude Code that finds the key there bills the API."
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "pymux").mkdir()
    (tmp_path / "pymux" / "anthropic-api-key").write_text("sk-test\n")
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks))
        await plugin.run()
    conversation = plugin._conversation()
    assert conversation._settings["api_key"] == "sk-test"
    assert "ANTHROPIC_API_KEY" not in os.environ


async def test_another_program_and_a_sub_record_are_left_alone(quick):
    conversation = ListConversation([])
    await _heard(conversation, "state=done:app=codex", "state=done:id=job:app=claude-code")
    assert conversation.seen == []


async def test_after_the_most_nudges_the_pane_is_left_alone(quick, monkeypatch):
    monkeypatch.setattr(module, "MOST_NUDGES", 2)
    go_on = Verdict(decision=Decision.CONTINUE, reason="r", message="Go on.")
    conversation = ListConversation([go_on, go_on, go_on])
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks), lambda: conversation)  # type: ignore[arg-type]
        for _ in range(3):
            await plugin.on_program_reported(ProgramReported(PaneId(1), DONE))
            await anyio.sleep(0.1)
    assert len(quick) == 2
