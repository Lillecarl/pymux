"""
The agent a command comes from, as one packet field.

An agent runs `pymux run ...` with its session id in its environment,
under a name of its harness's choosing. The server cannot read that
environment: it started once, long before, and its own variables name
nothing about this caller. So the client reads its own, on every
invocation while it is still fresh, and sends the answer under one
name both sides agree on: `PYMUX_AGENTIC_ID`, inside the
`environment` field the protocol already carries on every
`run-command` packet.

A value already sitting in `PYMUX_AGENTIC_ID` passes straight
through: that is the documented override, and it wins over every
harness variable below. Nothing set anywhere means an anonymous
caller, and the packet says so by saying nothing: an empty dict.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping

__all__ = [
    "AGENT_SESSION_VARS",
    "PYMUX_AGENTIC_ID",
    "caller_cwd",
    "caller_environment",
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


def caller_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    The caller session, as a packet field value.

    Answers `{PYMUX_AGENTIC_ID: id}` for the first variable above that
    holds a non-empty value, or `{}` when none does. Empty strings do
    not count: a harness that sets its variable to nothing is the same
    as one that sets nothing at all.
    """
    if env is None:
        env = os.environ
    for var in AGENT_SESSION_VARS:
        value = env.get(var)
        if value:
            return {PYMUX_AGENTIC_ID: value}
    return {}


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
