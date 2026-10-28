"""
Running a pymux command from a test, the ways a test needs to.

A key binding does not wait for what its command started, but a test
that did not would assert before the window or the pane exists. The
end state is the same either way, so every helper here waits.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app

from pymux.answer import Answer
from pymux.commands import call_command_handler, handle_command


async def run(pymux, command: str) -> None:
    "Run a command, and wait for what it started."
    answer = handle_command(pymux, command)
    if answer is not None:
        await answer


async def run_as(pymux, state, command: str) -> None:
    "Run a command as the person at this client, and wait for it."
    with set_app(state.app):
        await run(pymux, command)


async def answered(pymux, state, command: str, caller=None) -> Answer:
    "Run a command the way a client that waits for it does, and give back its answer."
    pymux.answer = answer = Answer(caller=caller)
    try:
        await run_as(pymux, state, command)
    finally:
        pymux.answer = None
    return answer


async def said(pymux, command: str) -> list:
    "Run a command, and give back what it wrote to the message log."
    before = len(pymux.message_log)
    await run(pymux, command)
    return list(pymux.message_log)[before:]


def complaints(pymux, command: str) -> list:
    """
    Run a command, and give back what it complained about.

    A command that fails does not raise. It records the message, which
    is how a line of a configuration file reaches the first client
    (Lillecarl/pymux#38), and `startup_errors` is where it lands
    before the startup is done.
    """
    before = len(pymux.startup_errors)
    handle_command(pymux, command)
    return pymux.startup_errors[before:]


async def over_socket(pymux, command: str, *arguments: str) -> list:
    "Run a command the way the socket does, and give back its errors."
    errors: list = []
    pymux.add_command_error = errors.append
    pymux.show_message = lambda message: None
    pymux.answer = Answer()
    try:
        answer = call_command_handler(command, pymux, list(arguments))
        if answer is not None:
            await answer
    finally:
        pymux.answer = None
    return errors
