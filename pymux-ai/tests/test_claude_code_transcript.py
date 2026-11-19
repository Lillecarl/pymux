"""
The background work a Claude Code transcript says is open, in the
shapes Claude Code writes: a tool result opens a task, and a queued
`<task-notification>` or a TaskStop closes it.
"""

from __future__ import annotations

import json
import os

from pymux_ai.claude_code_encourager.transcript import TaskKind, open_tasks, tasks_under

STARTED = "2026-11-19T12:00:00.000Z"
#: The epoch of `STARTED`.
AT = 1795089600.0


def _use(use_id: str, name: str, given: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": STARTED,
            "message": {"content": [{"type": "tool_use", "id": use_id, "name": name, "input": given}]},
        }
    )


def _result(use_id: str, result: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": STARTED,
            "toolUseResult": result,
            "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": "running"}]},
        }
    )


def _notification(task_id: str, status: str) -> str:
    return json.dumps(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": STARTED,
            "content": "<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n</task-notification>"
            % (task_id, status),
        }
    )


def _command(task_id: str, description: str, **given: object) -> list[str]:
    use = "use-" + task_id
    return [
        _use(use, "Bash", {"command": "nix build", "description": description, "run_in_background": True, **given}),
        _result(use, {"stdout": "", "backgroundTaskId": task_id}),
    ]


def test_a_background_command_is_open_until_its_notification():
    lines = [*_command("b1", "Run the gate"), *_command("b2", "Run the export")]
    (task,) = open_tasks([*lines, _notification("b1", "completed")], AT + 10)
    assert (task.task_id, task.kind, task.description) == ("b2", TaskKind.COMMAND, "Run the export")
    assert str(task) == 'command "Run the export"'


def test_a_running_notification_closes_nothing_and_a_task_stop_does():
    lines = [*_command("b1", "Run the gate"), _notification("b1", "running")]
    assert [task.task_id for task in open_tasks(lines, AT + 10)] == ["b1"]
    assert open_tasks([*lines, _use("stop", "TaskStop", {"task_id": "b1"})], AT + 10) == []


def test_a_task_past_its_own_timeout_and_grace_is_not_waited_on():
    "A monitor never says it ended: its timeout is all that closes it."
    lines = [
        _use("m", "Monitor", {"description": "watch CI"}),
        _result("m", {"taskId": "m1", "timeoutMs": 60_000}),
        *_command("b1", "Short build", timeout=30_000),
    ]
    assert {task.task_id for task in open_tasks(lines, AT + 89)} == {"m1", "b1"}
    assert {task.task_id for task in open_tasks(lines, AT + 100)} == {"m1"}
    assert open_tasks(lines, AT + 121) == []


def test_an_async_agent_is_waited_on_and_a_sync_one_is_not():
    lines = [
        _use("a", "Agent", {"description": "review the page"}),
        _result("a", {"agentId": "ag1", "isAsync": True}),
        _use("s", "Agent", {"description": "inline"}),
        _result("s", {"agentId": "ag2"}),
    ]
    assert [(task.task_id, task.kind) for task in open_tasks(lines, AT)] == [("ag1", TaskKind.AGENT)]


def test_the_transcript_is_found_through_the_process_and_its_session_file(tmp_path):
    "The pane's own process, here this test's, wrote `sessions/<pid>.json`."
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / ("%d.json" % os.getpid())).write_text(json.dumps({"sessionId": "s-1"}))
    project = tmp_path / "projects" / "-work"
    project.mkdir(parents=True)
    (project / "s-1.jsonl").write_text("\n".join(_command("b1", "Run the gate")) + "\n")

    assert [task.task_id for task in tasks_under(os.getpid(), AT, tmp_path)] == ["b1"]
    (tmp_path / "sessions" / ("%d.json" % os.getpid())).unlink()
    assert tasks_under(os.getpid(), AT, tmp_path) == []
