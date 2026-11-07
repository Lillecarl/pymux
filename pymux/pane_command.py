"""
What a pane runs, by tmux's rule. A command given as one string is
shell syntax, and runs as `default-shell -c <string>`. A command given
as several words is the program and its arguments, and runs as they
stand. Lillecarl/pyterm#935.

No server import here: the command line reads this before it knows
whether it starts one.
"""

from __future__ import annotations

import shlex

__all__ = ["PaneCommand", "argv_of", "pane_command", "spelled"]

PaneCommand = str | list[str]


def pane_command(words: list[str]) -> PaneCommand | None:
    "What the words of a command line name: one is a string for the shell, several are a list."
    if not words or words == [""]:
        return None
    return words[0] if len(words) == 1 else list(words)


def argv_of(command: PaneCommand | None, shell: str) -> list[str]:
    "What the pane forks: the shell alone, the shell with `-c` and a string, or the words."
    if command is None:
        return [shell]
    if isinstance(command, str):
        return [shell, "-c", command]
    return list(command)


def spelled(command: PaneCommand) -> str:
    "The command as a person reads it, on a title."
    return command if isinstance(command, str) else shlex.join(command)
