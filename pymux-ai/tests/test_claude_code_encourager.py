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
from pymux_ai.claude_code_encourager import transcript
from pymux_ai.claude_code_encourager.encourager import Encourager
from pymux_ai.claude_code_encourager.haiku import COMPACT_AT, HaikuConversation
from pymux_ai.claude_code_encourager.transcript import Task, TaskKind
from pymux_ai.claude_code_encourager.verdict import INSTRUCTIONS, Decision, Verdict, describe
from pymux_ai.observe import Observation
from pyte.program_status import Report, parse_report

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


async def test_with_no_font_to_draw_with_the_text_is_judged_alone(tmp_path):
    model = ListModel([_verdict(Decision.STOP)])
    conversation = HaikuConversation("key", model, AttachmentStore(tmp_path))

    await conversation.judge(Observation("%1", DONE, "/work", "> done", None, ()))

    (sent,) = model.asked
    assert not any(isinstance(part, AttachmentPart) for message in sent for part in message.parts)


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

    def __init__(self, tasks: anyio.abc.TaskGroup, front: bool = False) -> None:
        self._tasks = tasks
        self.log = logging.getLogger("test-encourager")
        self.messages: list[str] = []
        self.pymux = self
        self.front = front
        self.reports: dict[PaneId, Report] = {}

    def in_front(self, pane_id: PaneId) -> bool:
        return self.front

    def program_reports(self) -> dict[PaneId, Report]:
        return self.reports

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

    async def type_(self, pane, text, keys=()):
        typed.append((pane, text, *keys))

    async def background_of(host, pane_id):
        return ()

    monkeypatch.setattr("pymux_ai.observe.observe", observe)
    monkeypatch.setattr(transcript, "background_of", background_of)
    monkeypatch.setattr(Encourager, "_type", type_)
    return typed


@pytest.fixture
def building(quick, monkeypatch):
    "An agent with a background build open, and a short wait for it."
    monkeypatch.setattr(module, "BACKGROUND_WAIT", 0.3)
    build = Task("b1", TaskKind.COMMAND, "Run the gate", float("inf"))

    async def background_of(host, pane_id):
        return (build,)

    monkeypatch.setattr(transcript, "background_of", background_of)
    return quick


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


async def test_a_look_waits_on_background_work_and_names_it_to_the_model(building):
    conversation = ListConversation([Verdict(decision=Decision.STOP, reason="it waits on the gate")])
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks), lambda: conversation)  # type: ignore[arg-type]
        await plugin.on_program_reported(ProgramReported(PaneId(1), DONE))
        await anyio.sleep(0.15)
        assert conversation.seen == []
    (seen,) = conversation.seen
    assert seen.waiting_on == ('command "Run the gate"',)
    assert 'Its own background tasks it has not heard the end of: command "Run the gate".' in describe(seen)


async def test_the_work_ending_wakes_the_agent_and_drops_the_look(building):
    "The notification starts a turn, which Claude Code reports as `working`."
    conversation = ListConversation([])
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks), lambda: conversation)  # type: ignore[arg-type]
        await plugin.on_program_reported(ProgramReported(PaneId(1), DONE))
        await anyio.sleep(0.1)
        await plugin.on_program_reported(ProgramReported(PaneId(1), parse_report("state=working:app=claude-code")))
    assert (conversation.seen, building) == ([], [])


#: A Claude Code question, as vivarium's agent put one, cut short.
QUESTION_SCREEN = """\
 ☐ Before land

│ Which runs before I land?

❯ 1. Run all four, then land (Recommended)
     dinix.checks, nixkube umlTest, easykubenix kubeapply and vivarium k8s.
  2. Run dinix and k8s only
  3. Land now
  4. Type something.
─────────────────────────────────────
  5. Chat about this
"""

QUESTION = "state=blocked:kind=question:app=claude-code"


def test_the_field_for_free_text_is_reached_from_the_first_option():
    assert module.keys_to_free_text(QUESTION_SCREEN) == ["Down"] * 3
    assert module.keys_to_free_text("> done") is None


@pytest.fixture
def asked(quick, monkeypatch):
    "A question on the screen, and short waits for it."
    monkeypatch.setattr(module, "QUESTION_WAIT", 0.1)
    monkeypatch.setattr(module, "QUESTION_LONGEST", 0.35)

    async def observe(host, pane_id, report):
        return Observation("%1", report, "/work", QUESTION_SCREEN, PNG, ())

    monkeypatch.setattr("pymux_ai.observe.observe", observe)
    return quick


async def test_a_question_left_unanswered_is_given_back_to_the_agent(asked):
    "The fixed text, and never the model's words: the encourager picks no option."
    defer = Verdict(decision=Decision.CONTINUE, reason="a test plan the agent owns", message="Take option 2.")
    conversation = ListConversation([defer])
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks), lambda: conversation)  # type: ignore[arg-type]
        await plugin.on_program_reported(ProgramReported(PaneId(1), parse_report(QUESTION)))
        await anyio.sleep(0.05)
        assert conversation.seen == []
    assert asked == [("%1", "[encourager] " + module.DEFER, "Down", "Down", "Down")]
    assert conversation.seen[0].report.state == "blocked"


async def test_a_question_the_model_keeps_for_the_person_types_nothing(asked):
    conversation = ListConversation([Verdict(decision=Decision.STOP, reason="it asks about a push")])
    await _heard(conversation, QUESTION)
    assert asked == []


async def test_a_question_waits_longer_while_a_person_has_the_pane_in_front(asked):
    conversation = ListConversation([Verdict(decision=Decision.CONTINUE, reason="r")])
    async with anyio.create_task_group() as tasks:
        plugin = Encourager(Host(tasks, front=True), lambda: conversation)  # type: ignore[arg-type]
        await plugin.on_program_reported(ProgramReported(PaneId(1), parse_report(QUESTION)))
        await anyio.sleep(0.2)
        assert conversation.seen == []
    assert len(asked) == 1


async def test_an_answered_question_drops_the_look(asked):
    conversation = ListConversation([])
    await _heard(conversation, QUESTION, "state=working:app=claude-code")
    assert (conversation.seen, asked) == ([], [])


async def test_a_question_put_before_the_start_is_picked_up_and_a_done_is_not(asked):
    "As after an upgrade: the plugin before this one looked at the `done` already."
    conversation = ListConversation([Verdict(decision=Decision.CONTINUE, reason="r")])
    async with anyio.create_task_group() as tasks:
        host = Host(tasks)
        host.reports = {PaneId(1): parse_report(QUESTION), PaneId(2): DONE}
        await Encourager(host, lambda: conversation).run()  # type: ignore[arg-type]
    assert [seen.pane for seen in conversation.seen] == ["%1"]
    assert len(asked) == 1


async def test_a_permission_prompt_is_never_answered(asked):
    conversation = ListConversation([])
    await _heard(conversation, "state=blocked:kind=permission:app=claude-code")
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
