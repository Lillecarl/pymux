"""
libpymux holds libtmux's API: more, never less, never different.

For each of `Server`, `Session`, `Window` and `Pane`, every public member
of libtmux's class has to be on libpymux's, as the same kind of thing:

- a value (a dataclass field, an attribute or a property) is a value
  here too, with the same annotation;
- a method is a method, and every parameter libtmux's takes is here
  with the same name, kind, default and annotation. A parameter libtmux
  does not have is allowed when it has a default.

What libpymux adds is not judged. `async_objects.py` is not judged
either: a property cannot be awaited, so it cannot match.

**`libtmux_gaps.txt` is a ratchet.** It lists every difference there is
today, one per line. A new difference fails, and so does a line whose
difference has gone, so the list only shrinks. After closing a gap, run
this file as a script and write what it prints over the list:

    python tests/test_libpymux_is_libtmux.py > tests/libtmux_gaps.txt

Lillecarl/pymux#1 is the API this keeps honest.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import sys
from pathlib import Path

import libpymux
import libtmux

GAPS = Path(__file__).with_name("libtmux_gaps.txt")

CLASSES = ("Server", "Session", "Window", "Pane")

#: Module prefixes that spell one type two ways: `t.Any` and `Any`.
_PREFIXES = re.compile(r"\b(?:t|typing|pathlib|collections\.abc)\.")


def _type(annotation: object) -> str:
    "An annotation as comparable text."
    if annotation is inspect.Parameter.empty:
        return ""
    if not isinstance(annotation, str):
        annotation = inspect.formatannotation(annotation)
    return _PREFIXES.sub("", annotation).replace(" ", "")


def _declared(cls: type) -> dict[str, object]:
    "Every annotation the class and its bases declare, nearest first."
    found: dict[str, object] = {}
    for klass in reversed(cls.__mro__):
        found.update(inspect.get_annotations(klass))
    return found


def _members(cls: type) -> dict[str, tuple[str, object]]:
    """
    The public members of `cls`, as name to (kind, what to compare).

    A value compares by its annotation, a method by its signature.
    """
    found: dict[str, tuple[str, object]] = {}
    declared = _declared(cls)
    fields = {field.name: field.type for field in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else {}
    for name in sorted(set(dir(cls)) | set(declared) | set(fields)):
        if name.startswith("_"):
            continue
        raw = inspect.getattr_static(cls, name, None)
        if isinstance(raw, property):
            found[name] = ("value", _type(inspect.signature(raw.fget).return_annotation))
        elif isinstance(raw, (staticmethod, classmethod)) or inspect.isfunction(raw):
            found[name] = ("method", inspect.signature(getattr(cls, name)))
        else:
            found[name] = ("value", _type(fields.get(name, declared.get(name, inspect.Parameter.empty))))
    return found


def _parameters(signature: inspect.Signature) -> list[inspect.Parameter]:
    return [one for one in signature.parameters.values() if one.name not in ("self", "cls")]


def _method_gaps(theirs: inspect.Signature, ours: inspect.Signature) -> list[str]:
    gaps = []
    our_parameters = {one.name: one for one in _parameters(ours)}
    for their in _parameters(theirs):
        our = our_parameters.pop(their.name, None)
        if our is None:
            gaps.append("parameter %s is missing" % (their.name,))
            continue
        if our.kind != their.kind:
            gaps.append("parameter %s is %s, not %s" % (their.name, our.kind.name, their.kind.name))
        if repr(our.default) != repr(their.default):
            gaps.append("parameter %s defaults to %r, not %r" % (their.name, our.default, their.default))
        if _type(our.annotation) != _type(their.annotation):
            gaps.append("parameter %s is %s, not %s" % (their.name, _type(our.annotation), _type(their.annotation)))
    for extra in our_parameters.values():
        if extra.default is inspect.Parameter.empty and extra.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            gaps.append("parameter %s has no default and libtmux has no such parameter" % (extra.name,))
    if _type(ours.return_annotation) != _type(theirs.return_annotation):
        gaps.append("returns %s, not %s" % (_type(ours.return_annotation), _type(theirs.return_annotation)))
    return gaps


def gaps() -> list[str]:
    "Every way libpymux's objects fall short of libtmux's, one line each."
    found = []
    for name in CLASSES:
        theirs = _members(getattr(libtmux, name))
        ours = _members(getattr(libpymux, name))
        for member, (their_kind, their_shape) in theirs.items():
            where = "%s.%s" % (name, member)
            if member not in ours:
                found.append("%s: missing" % (where,))
                continue
            our_kind, our_shape = ours[member]
            if our_kind != their_kind:
                found.append("%s: a %s, not a %s" % (where, our_kind, their_kind))
            elif their_kind == "value":
                if our_shape != their_shape:
                    found.append("%s: %s, not %s" % (where, our_shape or "unannotated", their_shape))
            else:
                found += ["%s: %s" % (where, gap) for gap in _method_gaps(their_shape, our_shape)]
    return sorted(found)


def test_libpymux_holds_libtmuxs_api():
    recorded = [line for line in GAPS.read_text().splitlines() if line and not line.startswith("#")]
    now = gaps()
    new = sorted(set(now) - set(recorded))
    closed = sorted(set(recorded) - set(now))
    assert not new, "libpymux differs from libtmux %s in a new way:\n%s" % (
        libtmux.__version__,
        "\n".join(new),
    )
    assert not closed, "these gaps are closed; take them out of %s:\n%s" % (GAPS.name, "\n".join(closed))


def test_the_gaps_are_compared_against_a_known_libtmux():
    "The list is only true of one libtmux: a new one wants a new list."
    first = GAPS.read_text().splitlines()[0]
    assert first == "# libtmux %s" % (libtmux.__version__,), first


if __name__ == "__main__":
    sys.stdout.write("# libtmux %s\n" % (libtmux.__version__,))
    sys.stdout.writelines(line + "\n" for line in gaps())
