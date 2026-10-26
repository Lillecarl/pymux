"""
One declaration per option: a field of its scope's dataclass.

`option()` declares one, and `@options` makes the class. The option's
name is the field's with `-` for `_`, and its type is the default's.
The rest is derived from that one place: what `set-option` parses, what
Tab offers, what `show-options` prints, the hot upgrade's fate, and the
snapshot's column. Lillecarl/pymux#769.

**A value is a plain slot.** A frame reads several options, so a read
must cost what an attribute costs. Only a write runs code, and only for
an option that declares `on_write`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import MISSING, dataclass, field, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from pyte.keep import Keep

if TYPE_CHECKING:
    from pymux.main import Pymux

__all__ = ["Declared", "Options", "option", "options"]

_KEY = "option"


@dataclass(frozen=True, slots=True)
class _Said:
    "What `option()` was told, until `@options` knows the field's name."

    doc: str
    choices: Choices
    parse: Callable[[str], Any] | None
    on_set: Callable[[Pymux, Any], None] | None
    on_write: Callable[[Any], None] | None
    show: Callable[[Any, Any], str] | None
    keep: Keep


#: What Tab offers: a fixed list, or a function that lists them in the
#: order to offer them, called each time.
type Choices = tuple[str, ...] | Callable[[], Iterable[str]]


def option(
    default: Any = MISSING,
    *,
    doc: str,
    factory: Callable[[], Any] | None = None,
    choices: Iterable[object] | Callable[[], Iterable[str]] = (),
    parse: Callable[[str], Any] | None = None,
    on_set: Callable[[Pymux, Any], None] | None = None,
    on_write: Callable[[Any], None] | None = None,
    show: Callable[[Any, Any], str] | None = None,
    keep: Keep = Keep.SAVED,
) -> Any:
    """
    Declare one option.

    :param factory: Makes the default when an owner is made, in place
        of `default`: one that reads the environment, such as `$SHELL`.
    :param choices: What Tab offers besides the value it holds. A
        boolean or an enum offers its own values. A function is called
        each time, for a list that is costly to make or that grows.
    :param parse: The written value to what the option holds, or
        `ValueError` with what it takes. Without it the default's type
        decides.
    :param on_set: What the server does after a command set it, with
        the server and the object the options belong to.
    :param on_write: What the owner does after any write: an invariant
        of its own, which no caller can skip.
    :param show: What `show-options` prints for a value, given the
        value and its owner, where the value alone does not say it all.
    :param keep: What a hot upgrade does with it. An option whose truth
        lives elsewhere -- the logger's level, a kernel setting -- is
        `REBUILT`, and the snapshot has no column for it.
    """
    listed = choices if callable(choices) else tuple(str(one) for one in choices)
    said = _Said(doc, listed, parse, on_set, on_write, show, keep)
    if factory is not None:
        return field(default_factory=factory, metadata={_KEY: said})
    return field(default=default, metadata={_KEY: said})


def _parse_bool(value: str) -> bool:
    value = value.lower()
    if value not in ("on", "off"):
        raise ValueError('Expecting "on" or "off".')
    return value == "on"


def _parse_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise ValueError("Expecting an integer.")
    return number


def _parse_positive(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = 0.0
    if not number > 0 or math.isinf(number):
        raise ValueError("Expecting a number above zero.")
    return number


def _parse_member(kind: type[StrEnum]) -> Callable[[str], StrEnum]:
    def parse(value: str) -> StrEnum:
        try:
            return kind(value)
        except ValueError:
            raise ValueError("Expecting one of: %s." % ", ".join('"%s"' % one for one in kind)) from None

    return parse


def _parse_of(default: Any) -> Callable[[str], Any]:
    "How a written value becomes one of `default`'s type."
    if isinstance(default, bool):
        return _parse_bool
    if isinstance(default, StrEnum):
        return _parse_member(type(default))
    if isinstance(default, int):
        return _parse_count
    if isinstance(default, float):
        return _parse_positive
    if isinstance(default, str):
        return str
    raise TypeError("an option holds a bool, an int, a float, a str or a StrEnum, not %r" % (default,))


@dataclass(frozen=True, slots=True)
class Declared:
    "One option, as its field declares it."

    #: As a person writes it: `monitor-activity`.
    name: str
    #: The field: `monitor_activity`.
    attribute: str
    default: Any
    doc: str
    choices: Choices
    parse: Callable[[str], Any]
    on_set: Callable[[Pymux, Any], None] | None
    show: Callable[[Any, Any], str] | None
    keep: Keep

    def values(self, now: Any = None) -> list[str]:
        "What Tab offers: the choices, and the value it holds now."
        if isinstance(self.default, bool):
            return ["on", "off"]
        if isinstance(self.default, StrEnum):
            return [str(one) for one in type(self.default)]
        if callable(self.choices):
            listed = list(self.choices())
            if now is not None and str(now) not in listed:
                listed.append(str(now))
            return listed
        offered = set(self.choices)
        if now is not None:
            offered.add(str(now))
        return sorted(offered)

    def stored(self, value: Any) -> Any:
        "`value` as sqlite holds it."
        return value.value if isinstance(value, StrEnum) else value

    def loaded(self, value: Any) -> Any:
        "A value sqlite gave back, in the option's own type."
        if isinstance(self.default, bool):
            return bool(value)
        if isinstance(self.default, StrEnum):
            return type(self.default)(value)
        return value

    def python_type(self) -> type:
        "What sqlite holds it as."
        if isinstance(self.default, StrEnum):
            return str
        if isinstance(self.default, (bool, int)):
            return int
        return type(self.default)

    def column_type(self) -> str:
        "The type affinity of its snapshot column."
        return {int: "INTEGER", float: "REAL"}.get(self.python_type(), "TEXT")


class Options:
    """
    The base of one scope's options. `owner` is what holds them: a
    window holds its `WindowOptions`.
    """

    __slots__ = ("owner",)

    #: Each option by the name a person writes.
    DECLARED: ClassVar[dict[str, Declared]]
    _ON_WRITE: ClassVar[dict[str, Callable[[Any], None]]]
    KEEP: ClassVar[dict[str, Keep]]

    owner: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", None)

    def hold(self, name: str, value: Any) -> None:
        "Take a value the owner already acts on, without its `on_write`."
        object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        hook = self._ON_WRITE.get(name)
        if hook is not None:
            owner = getattr(self, "owner", None)
            if owner is not None:
                hook(owner)


def options[T: Options](cls: type[T]) -> type[T]:
    """
    Make `cls` a scope's options, from the fields its `option()` calls
    declare. Every field has a default, so a checker that does not see
    the dataclass loses nothing: `cls()` takes no argument.
    """
    made = dataclass(slots=True)(cls)
    declared: dict[str, Declared] = {}
    on_write: dict[str, Callable[[Any], None]] = {}
    for each in fields(made):  # type: ignore[arg-type]
        said: _Said = each.metadata[_KEY]
        name = each.name.replace("_", "-")
        # A factory's default, as this process makes it, says the type.
        default = each.default if each.default is not MISSING else each.default_factory()  # type: ignore[misc]
        declared[name] = Declared(
            name=name,
            attribute=each.name,
            default=default,
            doc=said.doc,
            choices=said.choices,
            parse=said.parse or _parse_of(default),
            on_set=said.on_set,
            show=said.show,
            keep=said.keep,
        )
        if said.on_write is not None:
            on_write[each.name] = said.on_write
    made.DECLARED = declared
    made._ON_WRITE = on_write
    # The owner gives itself again.
    made.KEEP = {each.attribute: each.keep for each in declared.values()} | {"owner": Keep.REBUILT}
    return made
