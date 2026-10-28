"""
What a command says back to the client that sent it over the socket.

**A context variable, not an attribute of the server.** A handler may
await, so two commands of two clients can be in flight at once, and
one answer on the server would hand each of them the other's output.
A task copies the context that started it, so the answer a
`run-command` packet makes belongs to that packet alone.
Lillecarl/pymux#87.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field

from libpymux.agentic import CallerContext

__all__ = ["Answer", "current_answer"]


@dataclass
class Answer:
    """
    The answer of one `run-command` packet, while it is answered.

    No answer at all means a command nobody waits for: a key binding,
    the command bar, a configuration file. A listing then goes to the
    screen of the client, and an error to its message line.
    """

    #: Who sent the command, and from where. The job commands read it:
    #: `run` stamps the session and inherits the directory and the
    #: environment, and the lookups prefer the session.
    caller: CallerContext | None = None
    output: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: What the caller should know, on stderr, with no change to the
    #: exit code.
    warnings: list[str] = field(default_factory=list)
    #: Set by a command that reports another program's ending: `tail`
    #: with the job's exit, 124 past its timeout. `None` sends 1 when
    #: there are errors and 0 when there are none.
    exit_code: int | None = None

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


current_answer: contextvars.ContextVar[Answer | None] = contextvars.ContextVar("pymux-answer", default=None)
