"""
How big a source tree is, and where: its code, docstring, comment and
blank lines, and its biggest classes and functions. Tests are left out.
Lillecarl/pymux#775 measures against it.

    python tests/measure_the_code.py pymux ../pyte/pyte
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

BIGGEST = 6


def _is_docstring(node: ast.AST) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def measure(path: Path) -> tuple[int, int, int, int, list[tuple[int, str, str]]]:
    "Lines, docstring lines, comments, blank lines, and every class and function with its length."
    text = path.read_text()
    docstrings = 0
    units = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and _is_docstring(node.body[0]):
                docstrings += node.body[0].end_lineno - node.body[0].lineno + 1
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "func"
            units.append((node.end_lineno - node.lineno + 1, kind, "%s:%d %s" % (path, node.lineno, node.name)))
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    comments = sum(1 for token in tokens if token.type == tokenize.COMMENT)
    blank = sum(1 for line in text.splitlines() if not line.strip())
    return text.count("\n"), docstrings, comments, blank, units


def main(roots: list[str]) -> None:
    for root in roots:
        lines = docstrings = comments = blank = 0
        units = []
        for path in sorted(Path(root).rglob("*.py")):
            if "tests" in path.parts:
                continue
            a, b, c, d, found = measure(path)
            lines, docstrings, comments, blank = lines + a, docstrings + b, comments + c, blank + d
            units += found
        code = lines - docstrings - comments - blank
        print(
            "%s: %d lines, code %d, docstrings %d, comments %d, blank %d"
            % (root, lines, code, docstrings, comments, blank)
        )
        for kind in ("class", "func"):
            for size, _, name in sorted((unit for unit in units if unit[1] == kind), reverse=True)[:BIGGEST]:
                print("  %-5s %5d %s" % (kind, size, name))


if __name__ == "__main__":
    main(sys.argv[1:] or ["pymux"])
