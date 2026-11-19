"""
The background work a Claude Code agent waits on, read out of its
transcript: the same reading wrapty's Stop hook does.

The pane names a process, Claude Code writes `sessions/<pid>.json` with
its session id, and the transcript is `projects/*/<session id>.jsonl`.
Each background task opens with an id in a tool result and closes with
a `<task-notification>` in a terminal status, or a TaskStop call.

All but `background_of` blocks: psutil reads the process table, and a
transcript reaches megabytes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import psutil

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from pymux.ids import PaneId
    from pymux.plugin_api import Host

__all__ = ["Task", "TaskKind", "background_of", "open_tasks", "tasks_under", "transcript_under"]

#: The bound for a task that carries no timeout of its own: a transcript
#: resumed with --resume still lists tasks whose processes died with the
#: session before, and a task never closed would hold the pane forever.
DEFAULT_BOUND: Final = 3600.0

#: Added to every deadline: a task just past its timeout may still be
#: writing its notification.
GRACE: Final = 60.0

TERMINAL_STATUSES: Final = frozenset({"completed", "failed", "killed"})

#: A line holds one of these to be worth parsing; most of a transcript
#: is thrown away before `json.loads` sees it. The tool names keep the
#: use, which holds a task's description and a command's timeout.
_INTERESTING: Final = (
    "backgroundTaskId",
    "taskId",
    "agentId",
    "task-notification",
    "TaskStop",
    "run_in_background",
    "Monitor",
    '"Agent"',
)


class TaskKind(StrEnum):
    COMMAND = "command"
    MONITOR = "monitor"
    AGENT = "agent"


@dataclass(frozen=True, slots=True)
class Task:
    "A background task the agent started and has not heard the end of."

    task_id: str
    kind: TaskKind
    description: str
    deadline: float

    def __str__(self) -> str:
        return '%s "%s"' % (self.kind, self.description)


def config_dir() -> Path:
    "Where Claude Code keeps its state."
    named = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(named) if named else Path.home() / ".claude"


def transcript_under(pid: int, config: Path | None = None) -> Path | None:
    "The transcript of the Claude Code that `pid` is, or runs under it; None when none does."
    config = config_dir() if config is None else config
    try:
        root = psutil.Process(pid)
        pids = [pid, *(child.pid for child in root.children(recursive=True))]
    except psutil.Error:
        return None
    for one in pids:
        try:
            session = json.loads((config / "sessions" / ("%d.json" % one)).read_text())
        except OSError, ValueError:
            continue
        session_id = session.get("sessionId") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or "/" in session_id:
            continue
        for found in (config / "projects").glob("*/%s.jsonl" % session_id):
            return found
    return None


async def background_of(host: Host, pane_id: PaneId) -> tuple[Task, ...]:
    "What the Claude Code in pane `pane_id` waits on now."
    pane = host.pane(pane_id)
    pid = pane.process.backend.pid if pane is not None else None
    if not pid:
        return ()
    return tuple(await anyio.to_thread.run_sync(tasks_under, pid, time.time()))


def tasks_under(pid: int, now: float, config: Path | None = None) -> list[Task]:
    "What the Claude Code in the pane whose process is `pid` waits on."
    path = transcript_under(pid, config)
    if path is None:
        return []
    try:
        with path.open(errors="replace") as lines:
            return open_tasks(lines, now)
    except OSError:
        return []


def _epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _tag(text: str, name: str) -> str | None:
    start = text.find("<%s>" % name)
    end = text.find("</%s>" % name, start)
    if start < 0 or end < 0:
        return None
    return text[start + len(name) + 2 : end]


# A transcript line is JSON with no fixed shape: Claude Code's own format.
def _blocks(entry: dict[str, object]) -> Iterator[dict[str, object]]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        yield from (block for block in content if isinstance(block, dict))


def open_tasks(lines: Iterable[str], now: float) -> list[Task]:
    "The tasks a transcript started and has not closed, each before its deadline, oldest first."
    # A tool use's id, to its name and input: a result names its use by id.
    uses: dict[object, tuple[str, dict[str, object]]] = {}
    tasks: dict[str, Task] = {}
    for line in lines:
        if not any(token in line for token in _INTERESTING):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        started = _epoch(entry.get("timestamp")) or now
        for block in _blocks(entry):
            if block.get("type") != "tool_use":
                continue
            given = block.get("input")
            given = given if isinstance(given, dict) else {}
            uses[block.get("id")] = (str(block.get("name") or ""), given)
            # The one call whose request, and not a notification, closes a task.
            if block.get("name") == "TaskStop":
                for key in ("task_id", "shell_id"):
                    tasks.pop(str(given.get(key)), None)

        result = entry.get("toolUseResult")
        if isinstance(result, dict):
            opened = _opened(result, entry, uses, started)
            if opened is not None:
                tasks[opened.task_id] = opened

        content = entry.get("content")
        if isinstance(content, str) and "<task-notification>" in content:
            if _tag(content, "status") in TERMINAL_STATUSES:
                tasks.pop(_tag(content, "task-id") or "", None)
    return [task for task in tasks.values() if task.deadline > now]


def _opened(
    result: dict[str, object],
    entry: dict[str, object],
    uses: dict[object, tuple[str, dict[str, object]]],
    started: float,
) -> Task | None:
    bound: float | None = None
    if result.get("backgroundTaskId"):
        task_id, kind = str(result["backgroundTaskId"]), TaskKind.COMMAND
    elif result.get("taskId"):
        task_id, kind = str(result["taskId"]), TaskKind.MONITOR
        timeout = result.get("timeoutMs")
        if isinstance(timeout, (int, float)) and timeout > 0:
            bound = timeout / 1000
    elif result.get("agentId") and result.get("isAsync"):
        task_id, kind = str(result["agentId"]), TaskKind.AGENT
    else:
        return None
    name, given = next(
        (uses[block["tool_use_id"]] for block in _blocks(entry) if block.get("tool_use_id") in uses), ("", {})
    )
    if bound is None:
        timeout = given.get("timeout")
        command_bound = timeout / 1000 if kind is TaskKind.COMMAND and isinstance(timeout, (int, float)) else 0
        bound = command_bound if command_bound > 0 else DEFAULT_BOUND
    description = result.get("description") or given.get("description") or name or kind.value
    return Task(task_id, kind, str(description), started + bound + GRACE)
