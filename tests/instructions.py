"""
Count the bytecode instructions that a piece of work costs.

A wall clock says how fast the machine is. It says nothing that another
machine agrees with, and it says nothing at all in a build sandbox next
to fifteen other jobs. So this counts instructions instead.

`sys.monitoring` reports one event for each bytecode instruction that
the interpreter runs. The number is exact and it is the same on every
run: the same code over the same bytes gives the same count, on any
machine, under any load. That makes it something a check can hold to a
budget.

Two things move a count that is not a change in the code:

- The version of Python. New bytecode is a new number, and the budgets
  are recorded again when the interpreter moves.
- The seed of the hash. `PYTHONHASHSEED` decides the order of a set,
  and an order decides a branch. The check pins it.

**`ptterm/tests/instructions.py` is the same file**, and the two are
not shared on purpose. A test helper is not part of a package, so
nothing installs it, and each check's sandbox holds only the tests of
the repository it judges. Copying twenty lines is cheaper than making
one of these repositories depend on the other's test tree.
"""

import sys

__all__ = ["count_instructions"]

#: The tool that this module registers as. `sys.monitoring` holds a few
#: slots and gives each a number; a profiler is the honest one to take,
#: and nothing else in a test run uses it.
_TOOL = sys.monitoring.PROFILER_ID


def count_instructions(work) -> int:
    """
    Run `work` and return the bytecode instructions it took.

    The count covers everything the call reaches, so it holds the work
    of pymux and of prompt_toolkit and of the standard library under
    them. It does not hold the work of a C extension, which runs no
    bytecode.
    """
    counted = 0

    def one_instruction(code, offset):
        nonlocal counted
        counted += 1

    sys.monitoring.use_tool_id(_TOOL, "pymux-instructions")
    try:
        sys.monitoring.register_callback(
            _TOOL, sys.monitoring.events.INSTRUCTION, one_instruction
        )
        sys.monitoring.set_events(_TOOL, sys.monitoring.events.INSTRUCTION)
        try:
            work()
        finally:
            sys.monitoring.set_events(_TOOL, 0)
            sys.monitoring.register_callback(
                _TOOL, sys.monitoring.events.INSTRUCTION, None
            )
    finally:
        sys.monitoring.free_tool_id(_TOOL)

    return counted
