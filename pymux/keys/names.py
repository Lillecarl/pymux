"""
The names of keys: which prompt_toolkit key a key of the keyboard is,
with its modifiers, and what a key the toolkit has no name for is
called instead. Tables and the lookups over them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from prompt_toolkit.keys import KeyName, Keys
from pyte.keys.events import FIRST_FUNCTIONAL_KEY, KeyCode, KeyEvent, Modifier

__all__ = [
    "ALT",
    "CODE_AND_FORM_OF",
    "CTRL",
    "KEYPAD",
    "KEYS_A_KEYBOARD_SPELLS_OUT",
    "KEY_BY_ITS_NAME",
    "LETTER_KEYS",
    "MODIFIERS_WITH_NO_MEMBER",
    "MODIFIER_NAMES",
    "NAME_OF_A_KEY",
    "SHIFT",
    "TILDE_KEYS",
    "Key",
    "Unnamed",
    "UnnamedReason",
    "apply_modifiers",
    "base_of",
    "event_named",
    "key_named",
    "name_of",
    "with_alt",
]


# The modifier bits, under the names this file has always used. pyte
# owns the values, because it reads the sequences that carry them.
SHIFT = Modifier.SHIFT


ALT = Modifier.ALT


CTRL = Modifier.CTRL


# ctrl+<char> legacy control codes. (ctrl+[ is the escape character; it
# is not in this table.)
_CTRL_KEYS = {
    **{chr(ord("a") + i): getattr(Keys, "Control%s" % chr(ord("A") + i)) for i in range(26)},
    " ": Keys.ControlSpace,
    "@": Keys.ControlAt,
    "\\": Keys.ControlBackslash,
    "]": Keys.ControlSquareClose,
    "^": Keys.ControlCircumflex,
    "_": Keys.ControlUnderscore,
    "/": Keys.ControlUnderscore,
    "2": Keys.ControlAt,
    "4": Keys.ControlBackslash,
    "5": Keys.ControlSquareClose,
    "6": Keys.ControlCircumflex,
    "7": Keys.ControlUnderscore,
}


#: How each modifier is written in the name of a key.
#:
#: The order is the order of the bits in the protocol, and it is fixed
#: so that one combination has one name. `c` and `s` are what
#: prompt_toolkit already writes, so `c-s-a` built here is the same
#: string as `Keys.ControlShiftA` and the two match each other.
#:
#: Alt is not here. prompt_toolkit spells it as two key presses, an
#: escape and the key, and that is what pymux keeps: `super-a` behind
#: an escape is alt and super on a.
#:
#: The locks are not here either. Caps lock on a letter is the
#: capital, which is a character and not a key of its own.
MODIFIER_NAMES = (
    (Modifier.SHIFT, "s"),
    (Modifier.CTRL, "c"),
    (Modifier.SUPER, "super"),
    (Modifier.HYPER, "hyper"),
    (Modifier.META, "meta"),
)


#: The modifiers that no `Keys` member names, so a key that carries one
#: needs a name made for it.
MODIFIERS_WITH_NO_MEMBER = Modifier.SUPER | Modifier.HYPER | Modifier.META


#: What the four keys that carry a C0 code are called in a name.
#:
#: Written out rather than read off the `Keys` member that names them.
#: `Keys.Enter` is an alias of `ControlM`, so its value is "c-m", and
#: "super-c-m" is a name nobody would guess or type.
_CONTROL_KEY_NAMES: dict[int, str] = {
    KeyCode.ESCAPE: "escape",
    KeyCode.ENTER: "enter",
    KeyCode.TAB: "tab",
    KeyCode.BACKSPACE: "backspace",
}


#: The `Keys` member behind a base name, for every key that has one.
#:
#: A `Keys` member is a string, and its value is the name: `Keys.Home`
#: is "home" and `Keys.F5` is "f5". So the table is the members, read
#: back the way `base_of` writes them.
#:
#: The four above are written in as well, because their members do not
#: name them. `Keys.Enter` is an alias of `ControlM` and its value is
#: "c-m", which is not a name anybody would type.
KEY_BY_ITS_NAME = {
    **{str.__str__(key): key for key in Keys},
    "escape": Keys.Escape,
    "enter": Keys.Enter,
    "tab": Keys.Tab,
    "backspace": Keys.Backspace,
}


#: The base name of a `Keys` member: the table above, read the other
#: way, with the four written-out names winning.
#:
#: A member that is an alias reads back as the key it is an alias of.
#: `Keys.Enter` is `ControlM` and reads back as "c-m", `Keys.Backspace`
#: is `ControlH` and reads back as "c-h". Neither is a name a person
#: would write, and neither reaches the branch of `key_named` that
#: knows what ctrl on that key means.
NAME_OF_A_KEY: dict[str, str] = {
    **{key: str.__str__(key) for key in Keys},
    **{KEY_BY_ITS_NAME[name]: name for name in _CONTROL_KEY_NAMES.values()},
}


def name_of(base: str, mods: int) -> KeyName:
    """
    The name of one key with its modifiers.

    The name is built and not looked up, because there is no list to
    look in: five modifiers over every key is more combinations than
    anybody would write down. `KeyName` exists for exactly this, and a
    name built here binds and matches like one prompt_toolkit ships.

    `base` is the key without its modifiers, as it is written: a
    letter, or the value of a `Keys` member such as "up" or "f5".
    """
    parts = [name for modifier, name in MODIFIER_NAMES if mods & modifier]
    parts.append(base)
    return KeyName("-".join(parts))


#: ctrl and shift on a letter. prompt_toolkit names these, and only a
#: terminal that says more than the legacy encoding sends one: there,
#: ctrl+a and ctrl+shift+a are the same control code.
#: Lillecarl/pymux#168.
_CTRL_SHIFT_LETTERS = {chr(ord("a") + i): getattr(Keys, "ControlShift%s" % chr(ord("A") + i)) for i in range(26)}


# Keys that use the "CSI 1 ; modifier <letter>" form.
LETTER_KEYS = {
    "A": Keys.Up,
    "B": Keys.Down,
    "C": Keys.Right,
    "D": Keys.Left,
    "H": Keys.Home,
    "F": Keys.End,
    "P": Keys.F1,
    "Q": Keys.F2,
    "R": Keys.F3,
    "S": Keys.F4,
}


# Keys that use the "CSI number ; modifier ~" form.
TILDE_KEYS = {
    2: Keys.Insert,
    3: Keys.Delete,
    5: Keys.PageUp,
    6: Keys.PageDown,
    15: Keys.F5,
    17: Keys.F6,
    18: Keys.F7,
    19: Keys.F8,
    20: Keys.F9,
    21: Keys.F10,
    23: Keys.F11,
    24: Keys.F12,
}


#: The keys a terminal spells out, rather than sending a character:
#: the arrows, Home and End, Insert and Delete, the two page keys and
#: the function row.
#:
#: **These are the keys a keyboard can leave out.** A laptop with no
#: Home key cannot make one, and a program that asks for it then cannot
#: be answered at all. That is what Lillecarl/pymux#220 is for, so a
#: completer offers these first.
#:
#: It is the two tables above, read for their keys. A list written
#: beside them would be a second list to keep right, which is what
#: Lillecarl/pymux#234 cost ctrl+Home.
KEYS_A_KEYBOARD_SPELLS_OUT = frozenset(str.__str__(key) for key in (*TILDE_KEYS.values(), *LETTER_KEYS.values()))


# Keypad keys (private use area). Plain key presses map to their base
# key; modified keypad keys are dropped.
KEYPAD = {
    57399: "0",
    57400: "1",
    57401: "2",
    57402: "3",
    57403: "4",
    57404: "5",
    57405: "6",
    57406: "7",
    57407: "8",
    57408: "9",
    57409: ".",
    57410: "/",
    57411: "*",
    57412: "-",
    57413: "+",
    57414: Keys.Enter,
    57415: "=",
    57417: Keys.Left,
    57418: Keys.Right,
    57419: Keys.Up,
    57420: Keys.Down,
}


@dataclass(frozen=True, slots=True)
class Unnamed:
    """
    A complete key sequence that pymux cannot name.

    It carries why, because a key no binding of pymux's can take gives
    a person no way to find out what happened. The parser writes the
    reason and the sequence to the log. Lillecarl/pymux#167.

    **Not a tuple.** A tuple is how the parser spells a key that
    arrives as several, so an `Unnamed` that was one would be taken
    apart and handed out as key presses.
    """

    reason: str


class UnnamedReason(StrEnum):
    "Why pymux has no name for a key. The text goes in the log."

    KEYPAD_WITH_A_MODIFIER = "a keypad key with ctrl or alt"
    KEY_THAT_WRITES_NOTHING = "a key of the private use area: a lock key, a modifier key, a media key or F13 upwards"
    CTRL_AND_A_CHARACTER = "ctrl and a character that has no control code"
    TILDE_KEY_WITH_NO_NAME = "a key of the tilde form that pymux cannot name"
    LETTER_KEY_WITH_NO_NAME = "a key of the letter form that pymux cannot name"
    MODIFIER_THIS_KEY_HAS_NO_NAME_FOR = "a modifier that this key has no name for"


#: A key that a name reads back as: a character, a `Keys` member, the
#: two keys of alt, or an `Unnamed` for a combination pymux cannot name.
Key = str | Keys | Unnamed | tuple[str | Keys, ...]


def _ctrl_mapping(char: str) -> Keys | None:
    """
    Legacy ctrl+<char> mapping.

    **One character, or nothing.** `"a" <= x <= "z"` says yes to "up"
    as readily as to "u", and the name built from it named nothing.
    """
    if len(char) != 1:
        return None
    lower = char.lower()
    if "a" <= lower <= "z":
        return getattr(Keys, "Control%s" % lower.upper())
    return _CTRL_KEYS.get(char)


def apply_modifiers(key: str | Keys, mods: int) -> Key:
    """
    Apply the modifier bits to a plain key. Returns an `Unnamed` when
    the combination has no prompt_toolkit representation.

    **It never returns None.** None means "this is not a key sequence"
    to `_get_match`, and the parser then takes the sequence apart and
    hands the pieces out as key presses. ctrl+escape went that way, so
    a pane read "[27;5u" as five keys.

    **A functional key is told apart by its type, not by `str`.** Every
    member of `Keys` is a string, so `isinstance(key, str)` says yes to
    all of them, and the modified form of a functional key was never
    read. ctrl+Up asked for `Keys.ControlUP`, which is not a name, and
    prompt_toolkit's own table is the only reason nothing raised: it
    matches every sequence that would have come here.
    """
    shift = bool(mods & SHIFT)
    alt = bool(mods & ALT)
    ctrl = bool(mods & CTRL)

    if not isinstance(key, Keys):
        if ctrl:
            ctrl_key = _ctrl_mapping(key)
            if ctrl_key is None:
                return Unnamed(UnnamedReason.CTRL_AND_A_CHARACTER)
            key = ctrl_key
        elif shift and key.isalpha():
            key = key.upper()
    elif ctrl or shift:
        modified = _modified_form_of(key, ctrl, shift)
        if modified is None:
            return Unnamed(UnnamedReason.MODIFIER_THIS_KEY_HAS_NO_NAME_FOR)
        key = modified

    if alt:
        return (Keys.Escape, key)
    return key


def _modified_form_of(key: Keys, ctrl: bool, shift: bool) -> Keys | None:
    """
    The `Keys` member for a functional key with ctrl or shift on it,
    and None when the toolkit names no such key.

    **prompt_toolkit writes the modifiers into the name**, so the
    member is built rather than looked up: `Keys.ControlShiftLeft`,
    `Keys.ControlF13`, `Keys.ShiftDelete`. A table of the combinations
    was here instead, it named twenty-two, and it left out the whole
    of shift and everything above F12. Lillecarl/pymux#234.

    The order is the most it can carry first. A key that has no form
    for both keeps the ctrl and drops the shift, which is the trade the
    legacy encoding makes anyway. A key that has no form for shift
    alone keeps the key: a program reading F5 is better served than one
    reading nothing.
    """
    wanted = ["Control" * ctrl + "Shift" * shift]
    if ctrl and shift:
        wanted.append("Control")
    if shift and not ctrl:
        wanted.append("")

    for prefix in wanted:
        modified = getattr(Keys, prefix + key.name, None)
        if modified is not None:
            return modified
    return None


def _code_and_final_of_name() -> dict[str, tuple[int, str]]:
    """
    The number and form of every key that has a name: `base_of` read
    the other way.

    The three tables that name a key off the wire are inverted rather
    than written again. A character is not here; its number is its code
    point.
    """
    named = {name: (code, "u") for code, name in _CONTROL_KEY_NAMES.items()}
    for code, key in TILDE_KEYS.items():
        named[str.__str__(key)] = (code, "~")
    for final, key in LETTER_KEYS.items():
        # The letter form always carries one: the final byte is the key.
        named[str.__str__(key)] = (1, final)
    return named


CODE_AND_FORM_OF = _code_and_final_of_name()


def _modifiers_written_into(name: str) -> tuple[int, str]:
    '`name_of` read back: "s-tab" is shift on tab.'
    mods = 0
    found = True
    while found:
        found = False
        for modifier, spelling in MODIFIER_NAMES:
            prefix = spelling + "-"
            if len(name) > len(prefix) and name.startswith(prefix):
                mods |= modifier
                name = name[len(prefix) :]
                found = True
                break
    return mods, name


def event_named(base: str, mods: int) -> KeyEvent:
    """
    The key event that a base key name and its modifiers are.

    `base` is what `base_of` writes, and this is its inverse.

    **A name goes to a pane as an event, never as bytes.** The round
    trip through the legacy encoding dropped whatever that encoding
    cannot carry, and a key with no legacy form came out as its own
    name. Lillecarl/pymux#237.

    Raises `ValueError` for a name that no key has.
    """
    known = CODE_AND_FORM_OF.get(base)
    if known is not None:
        code, final = known
        return KeyEvent(code, mods, final)

    carried, rest = _modifiers_written_into(base)
    if carried:
        # A base that carries its own modifiers, which is how the
        # toolkit names a few keys: `Keys.BackTab` is "s-tab". The
        # chord may hold more, and the two sets join.
        return event_named(rest, mods | carried)

    if len(base) == 1:
        # A character key. The protocol carries the key of the layout,
        # which is the unshifted one, and the shift is a modifier
        # beside it rather than a capital.
        if base.isupper():
            return KeyEvent(ord(base.lower()), mods | Modifier.SHIFT, "u")
        return KeyEvent(ord(base), mods, "u")

    raise ValueError("No key is named %r." % (base,))


def with_alt(key: str | Keys, mods: int) -> Key:
    """
    Alt on a key, which prompt_toolkit spells as two key presses.

    There is no `Keys` member for alt and no name for it either: an
    escape and the key is what the toolkit binds and what its parser
    feeds. So alt is the last thing applied, after the key it is on has
    its own name.
    """
    return (Keys.Escape, key) if mods & ALT else key


def key_named(base: str, mods: int) -> Key:
    """
    The prompt_toolkit key that one key with its modifiers is.

    `base` is the key without its modifiers, as `base_of` writes it: a
    character, or the value of a `Keys` member such as "up" or "f5", or
    one of the four C0 keys by name. `mods` is the bits of
    `pyte.keys.Modifier`.

    Returns a key, a character, a tuple of two for alt, or an `Unnamed`
    for a combination that prompt_toolkit cannot name.

    **This is the one translation, and both directions read it.** A key
    arriving off the wire comes here through `_named`, which reads the
    sequence and hands over the base and the bits. A key a person
    writes comes here through `key_spelling.py`, which reads the name
    and hands over the same two. Two translations of the same thing is
    what Lillecarl/pymux#119 cost 52 wrong keys, and what
    Lillecarl/pymux#234 cost ctrl+Home: the written form had a table of
    its own, and the table stopped at the four arrows.
    """
    if mods & MODIFIERS_WITH_NO_MEMBER:
        # super, hyper or meta. No `Keys` member names one of these and
        # no list could: five modifiers over every key is more
        # combinations than anybody would write down. So the name is
        # built, which is what `KeyName` is for. Lillecarl/pymux#181.
        return with_alt(name_of(base, mods & ~Modifier.ALT), mods)

    if base == "escape":
        return apply_modifiers(Keys.Escape, mods)

    if base == "enter":
        if mods & CTRL:
            # ctrl+enter is ctrl+j in the legacy encoding.
            return with_alt(Keys.ControlJ, mods)
        return apply_modifiers(Keys.Enter, mods)

    if base == "tab":
        if mods & SHIFT and not mods & (CTRL | ALT):
            return Keys.BackTab
        if mods & CTRL:
            return with_alt(Keys.ControlI, mods)
        return apply_modifiers(Keys.Tab, mods)

    if base == "backspace":
        if mods & CTRL:
            return with_alt(Keys.Backspace, mods)
        return apply_modifiers(Keys.Backspace, mods)

    if len(base) == 1:
        if mods & CTRL and mods & SHIFT:
            # Only a terminal that says more than the legacy encoding
            # can tell these apart: ctrl+a and ctrl+shift+a are one
            # control code there. Dropping the shift would leave a
            # person able to bind neither on its own. Lillecarl/pymux#168.
            both = _CTRL_SHIFT_LETTERS.get(base.lower())
            if both is not None:
                return with_alt(both, mods)
        return apply_modifiers(base, mods)

    member = KEY_BY_ITS_NAME.get(base)
    if member is None:
        return Unnamed(UnnamedReason.KEY_THAT_WRITES_NOTHING)
    return apply_modifiers(member, mods)


def base_of(event: KeyEvent) -> str | None:
    """
    The key of an event without its modifiers, as a name writes it.

    A letter or a digit is itself. A functional key is the value of
    the `Keys` member that names it, so "up" and "f5". None for a key
    that pymux cannot name at all, and then the modifiers cannot help.
    """
    if event.final == "u":
        # These four are written out, and not read off the `Keys`
        # member that names them. `Keys.Enter` is an alias of
        # `ControlM`, so its value is "c-m", and "super-c-m" is a
        # name nobody would guess or type.
        control_key = _CONTROL_KEY_NAMES.get(event.code)
        if control_key is not None:
            return control_key
        if event.code >= FIRST_FUNCTIONAL_KEY:
            keypad = KEYPAD.get(event.code)
            if keypad is None:
                return None
            return keypad if isinstance(keypad, str) else str.__str__(keypad)
        return chr(event.code).lower()
    if event.final == "~":
        tilde = TILDE_KEYS.get(event.code)
        return None if tilde is None else str.__str__(tilde)
    letter = LETTER_KEYS.get(event.final)
    return None if letter is None else str.__str__(letter)
