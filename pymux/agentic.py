"""
Who sent this command, and from where.

An agent runs `pymux run ...` with its session id in its environment,
under a name of its harness's choosing. The server cannot read that
environment: it started once, long before, and its own variables name
nothing about this caller. So the client sends its whole environment
on every `run-command` packet -- inside the `environment` field the
protocol already carries -- with the answer resolved beside it under
one name, and the directory it stands in under `Field.CWD`.

The names live here once, and both sides import them: a wire name
spelled by hand in two files matches nothing when it drifts, raises
nothing, and drops the meaning silently. Lillecarl/pymux#447.

A value already sitting in `PYMUX_AGENTIC_ID` passes straight
through: that is the documented override, and it wins over every
harness variable below. Nothing set anywhere means an anonymous
caller.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from typing import NamedTuple

__all__ = [
    "AGENT_SESSION_VARS",
    "PYMUX_AGENTIC_ID",
    "CallerContext",
    "caller_context",
    "caller_cwd",
    "caller_environment",
    "resolve_session_id",
]

#: The name the caller session id travels under.
PYMUX_AGENTIC_ID = "PYMUX_AGENTIC_ID"

#: Where an agent session id is looked for, first set wins. The
#: override itself is first, so an explicit value is never second to a
#: harness variable beside it.
AGENT_SESSION_VARS = (
    PYMUX_AGENTIC_ID,
    "OPENCODE_SESSION_ID",
    "OCAHUB_SESSION",
    "OC_SESSION",
    "CODEX_THREAD_ID",
    "TRAE_AI_SHELL_ID",
    "CURSOR_TRACE_ID",
)


def resolve_session_id(env: Mapping[str, object]) -> str | None:
    """
    The agent session id in this environment, or nothing.

    The first variable above holding a non-empty string wins. Empty
    strings do not count: a harness that sets its variable to nothing
    is the same as one that sets nothing at all. Anything not a
    string is not a value any harness set.
    """
    for var in AGENT_SESSION_VARS:
        value = env.get(var)
        if isinstance(value, str) and value:
            return value
    return None


def caller_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    The caller session, as a packet field value.

    Answers `{PYMUX_AGENTIC_ID: id}` for what `resolve_session_id`
    finds, or `{}` when it finds nothing. The client merges this over
    the whole environment it sends, so the canonical name is always
    there to read even though the id arrived under another.
    """
    if env is None:
        env = os.environ
    session_id = resolve_session_id(env)
    return {PYMUX_AGENTIC_ID: session_id} if session_id else {}


def caller_cwd() -> str | None:
    """
    The directory a command is sent from.

    Answers `None` when even that question fails -- a directory that
    was removed under the caller -- and then the server end falls back
    to its own, the way it always has.
    """
    with contextlib.suppress(OSError):
        return os.getcwd()
    return None


class CallerContext(NamedTuple):
    """
    Who sent one command, and from where.

    The server builds one per `run-command` packet and the commands
    read it while they run: `run` stamps the session onto the job and
    starts it in this directory with this environment, and the lookups
    prefer this session's jobs. Anything but a packet -- a key
    binding, the command bar, a configuration file -- runs without
    one, and then every default is the server's own, the way it has
    always been.
    """

    #: The agent session this command comes from, if any.
    session_id: str | None
    #: The caller's environment, as sent. Jobs start with it.
    environment: dict[str, str]
    #: The directory the command was sent from, if known.
    cwd: str | None


def caller_context(environment: Mapping[str, object] | None, cwd: object) -> CallerContext:
    """
    The caller context one packet carries.

    The wire may say anything, and a process environment may not, so
    only strings cross: anything else is dropped, not coerced, because
    a coerced value is a value nobody sent.
    """
    env = dict(environment) if environment else {}
    return CallerContext(
        session_id=resolve_session_id(env),
        environment={key: value for key, value in env.items() if isinstance(key, str) and isinstance(value, str)},
        cwd=cwd if isinstance(cwd, str) else None,
    )
