"""
Whether a group of a class's methods owns its state: each field the
group touches, and the methods outside it that touch that field too.

A group leaves the class as an object only when it owns its fields.
A field marked SHARED is read or written elsewhere; when the others
only read the server's registry (sessions, clients, options), a
reference to the server is enough. Lillecarl/pymux#775.

    python tests/who_owns_a_field.py pymux/main.py Pymux \\
        commands=handle_command,show_message,add_command_error
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

SHOWN = 6


def fields_by_method(path: str, class_name: str) -> dict[str, set[str]]:
    "Every method of the class, and the `self` attributes it touches."
    tree = ast.parse(Path(path).read_text())
    cls = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == class_name)
    found: dict[str, set[str]] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, set()).update(
                attribute.attr
                for attribute in ast.walk(node)
                if isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id == "self"
            )
    return found


def main(path: str, class_name: str, groups: list[str]) -> None:
    fields_of = fields_by_method(path, class_name)
    methods = set(fields_of)
    users: dict[str, set[str]] = defaultdict(set)
    for method, fields in fields_of.items():
        for field in fields - methods:
            users[field].add(method)

    for spec in groups:
        name, members = spec.split("=")
        group = set(members.split(","))
        touched = set().union(*(fields_of.get(member, set()) for member in group)) - methods
        print("== %s: %d methods, %d fields" % (name, len(group), len(touched)))
        for field in sorted(touched):
            outside = sorted(users[field] - group - {"__init__"})
            if not outside:
                print("  own    %s" % field)
                continue
            more = " ..." if len(outside) > SHOWN else ""
            print("  SHARED %s  <- %s%s" % (field, ", ".join(outside[:SHOWN]), more))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
