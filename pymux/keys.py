"""
The keyboard of the person, as a prompt_toolkit key press.

The outer terminal sends the extended forms when the client asked for
them, and for combinations that have no legacy encoding, such as
ctrl+enter, even when it did not. prompt_toolkit's input parser knows
the legacy encoding and none of the rest: without this the sequence
arrives garbled, one key press per character.

**The reading is pyte's.** `pyte.keys.parse_key_data` knows every mode
a keyboard can be in, and this file used to carry a second reader
beside it. Two readers of the same bytes cost 52 wrong keys once
(Lillecarl/pymux#119), so there is one. What is here is the naming:
which prompt_toolkit key a `KeyEvent` is, which pyte cannot say
because a toolkit sits above it.

The parser below extends prompt_toolkit's `Vt100Parser`. A key it
cannot name is consumed and written to the log, with the reason.

A key that came back up is not a key press, so it takes the key of a
release (`Keys.KeyRelease`) and keeps the sequence that the terminal
sent. A pane that asked for the event types of a key is given that
sequence; nothing else binds that key, so nothing else sees it.

The parser also swallows the string sequences that carry the replies of
the terminal queries: APC (`ESC _ ... ST`) for the graphics protocol,
DCS (`ESC P ... ST`) for the colour depth probe, OSC (`ESC ] ... ST`)
for the colour and clipboard answers, and the `CSI ... t` window report
for the cell size. Without this the replies would arrive as bursts of
key presses, and land in whichever pane has the focus.
"""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from prompt_toolkit.input.vt100_parser import (
    Vt100Parser,
    _IsPrefixOfLongerMatchCache,
)
from prompt_toolkit.key_binding.key_processor import _Flush
from prompt_toolkit.keys import KeyName, Keys

from pyte.keys import (
    FIRST_FUNCTIONAL_KEY,
    EventType,
    KeyCode,
    KeyEvent,
    Modifier,
    parse_key_data,
)

logger = logging.getLogger(__name__)

#: The bytes that introduce a control sequence. A key that arrives in
#: one is a key the outer terminal spelled out, and not a byte that
#: something else could still continue.
CSI = "\x1b["

__all__ = [
    "A_KEY_BY_ITS_NAME",
    "DropReason",
    "Dropped",
    "KEYS_A_KEYBOARD_SPELLS_OUT",
    "KittyVt100Parser",
    "THE_NAME_OF_A_KEY",
    "parse_kitty_key",
    "the_key_named",
]


# The modifier bits, under the names this file has always used. pyte
# owns the values, because it reads the sequences that carry them.
_SHIFT = Modifier.SHIFT
_ALT = Modifier.ALT
_CTRL = Modifier.CTRL

#: The shape of a complete key sequence, and nothing about what is in
#: it. This is a gate and not a reader: it says whether `parse_key_data`
#: is worth calling, and pulls no field out.
#:
#: It is here for speed. The parser of prompt_toolkit asks about every
#: prefix of what it holds, so a payload it is still buffering -- a
#: long OSC reply, a paste -- would be read from the start once per
#: character it grows by. That took the pymux suite from 18 seconds to
#: 228.
_LOOKS_LIKE_A_KEY_RE = re.compile(r"^\x1b\[[\d;:]*[u~ABCDEFHPQS]$")

#: A key whose modifier value is nine or more, which means a modifier
#: above ctrl: super, hyper or meta in the numbering of the protocol.
#: The value is one plus the sum of the bits, so ctrl and shift and
#: alt together are eight.
#:
#: It is a gate, the same as the one above, and it decides which table
#: reads the sequence rather than what the sequence means.
#: The third parameter is in here so that the modifyOtherKeys form,
#: "CSI 27 ; mods ; code ~", and the kitty form that carries the text
#: of a key are both covered.
_CARRIES_A_HIGH_MODIFIER_RE = re.compile(
    r"^\x1b\[[\d:]*;(\d+)[\d:]*(?:;[\d:]*)?[u~ABCDEFHPQS]$"
)

#: The largest modifier value the legacy numbering can mean: shift and
#: alt and ctrl together, which is one plus seven.
_CTRL_ALT_SHIFT = 8

# A prefix that could still become a kitty key sequence (or any other
# CSI sequence with variable parameters).
#
# "$" is in there for DECRQM, whose reply is "CSI ? <mode> ; <state> $ y".
# Without it the parser stops buffering at the "$" and hands the rest
# out as key presses.
_KITTY_PREFIX_RE = re.compile(r"^\x1b\[[0-9;:<=>?$]*$")

# An APC, DCS or OSC string sequence and any prefix of one. The payload
# never contains the escape character, so the terminator is
# unambiguous. (Both the two-character ST and the 8-bit ST are
# accepted.) An OSC ends at a bell as well, which is how most terminals
# close their colour answers; APC and DCS do not, so the two shapes
# stay apart.
_STRING_RE = re.compile(
    r"^(?:\x1b[_P][^\x1b\x9c]*(?:\x1b\\|\x9c)"
    r"|\x1b\][^\x1b\x9c\x07]*(?:\x1b\\|\x9c|\x07))\Z"
)
_STRING_PREFIX_RE = re.compile(
    r"^(?:\x1b[_P][^\x1b\x9c]*"
    r"|\x1b\][^\x1b\x9c\x07]*)\x1b?\Z"
)

# An unterminated string sequence must not swallow the input forever.
# The replies that we expect are a few dozen characters long.
MAX_STRING_LENGTH = 1024

#: How many key sequences with no name the parser remembers, so that a
#: held key writes one line to the log and not one per repeat. A
#: keyboard has fewer keys than this, and a set that grew without end
#: would be a leak on a stream of nonsense.
MAX_KEYS_TO_REMEMBER = 512

# ctrl+<char> legacy control codes. (ctrl+[ is the escape character; it
# is not in this table.)
_CTRL_KEYS = {
    **{
        chr(ord("a") + i): getattr(Keys, "Control%s" % chr(ord("A") + i))
        for i in range(26)
    },
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
_CONTROL_KEY_NAMES = {
    KeyCode.ESCAPE: "escape",
    KeyCode.ENTER: "enter",
    KeyCode.TAB: "tab",
    KeyCode.BACKSPACE: "backspace",
}


#: The `Keys` member behind a base name, for every key that has one.
#:
#: A `Keys` member is a string, and its value is the name: `Keys.Home`
#: is "home" and `Keys.F5` is "f5". So the table is the members, read
#: back the way `_base_of` writes them.
#:
#: The four above are written in as well, because their members do not
#: name them. `Keys.Enter` is an alias of `ControlM` and its value is
#: "c-m", which is not a name anybody would type.
A_KEY_BY_ITS_NAME = {
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
#: would write, and neither reaches the branch of `the_key_named` that
#: knows what ctrl on that key means.
THE_NAME_OF_A_KEY = {
    **{key: str.__str__(key) for key in Keys},
    **{A_KEY_BY_ITS_NAME[name]: name for name in _CONTROL_KEY_NAMES.values()},
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
_CTRL_SHIFT_LETTERS = {
    chr(ord("a") + i): getattr(Keys, "ControlShift%s" % chr(ord("A") + i))
    for i in range(26)
}

# Keys that use the "CSI 1 ; modifier <letter>" form.
_LETTER_KEYS = {
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
_TILDE_KEYS = {
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
KEYS_A_KEYBOARD_SPELLS_OUT = frozenset(
    str.__str__(key) for key in (*_TILDE_KEYS.values(), *_LETTER_KEYS.values())
)

# Keypad keys (private use area). Plain key presses map to their base
# key; modified keypad keys are dropped.
_KEYPAD = {
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
class Dropped:
    """
    A complete key sequence that pymux consumes and cannot name.

    It carries why, because a key that does nothing and leaves no
    trace gives a person no way to find out what happened. The parser
    writes the reason and the sequence to the log.
    Lillecarl/pymux#167.

    **Not a tuple.** A tuple is how the parser spells a key that
    arrives as several, so a `Dropped` that was one would be taken
    apart and handed out as key presses.
    """

    reason: str


class DropReason(StrEnum):
    "Why pymux has no name for a key. The text goes in the log."

    KEYPAD_WITH_A_MODIFIER = "a keypad key with ctrl or alt"
    A_KEY_THAT_WRITES_NOTHING = (
        "a key of the private use area: a lock key, a modifier key, a "
        "media key or F13 upwards"
    )
    CTRL_AND_A_CHARACTER = "ctrl and a character that has no control code"
    A_TILDE_KEY_WITH_NO_NAME = "a key of the tilde form that pymux cannot name"
    A_LETTER_KEY_WITH_NO_NAME = "a key of the letter form that pymux cannot name"
    A_MODIFIER_THIS_KEY_HAS_NO_NAME_FOR = "a modifier that this key has no name for"


# Sentinels for terminal replies that are not key events: the reply of
# the "CSI ? u" keyboard flags query, and a Primary/Secondary device
# attributes reply ("CSI ? ... c"). Both are used to detect whether the
# outer terminal supports the keyboard protocol.
_FLAGS_REPLY = object()
_DA1_REPLY = object()

# Sentinel for a complete APC, DCS or OSC string sequence. The kitty
# graphics protocol and the colour depth probe answer with one, and so
# does a terminal that answers a colour or clipboard query. They are
# reported like the other replies.
_STRING_REPLY = object()

# Sentinel for the "CSI 6 ; height ; width t" reply of the cell size
# query.
_CELL_SIZE_REPLY = object()

# Sentinel for the "CSI ? <mode> ; <state> $ y" reply of a DECRQM
# request. pymux asks whether the terminal holds a frame back while it
# is painted ("CSI ? 2026 $ p").
_MODE_REPLY = object()

# Sentinel for a key that came back up. The outer terminal sends one
# only when a pane asked for the event types of a key.
#
# A release is read before the key is: the sequence goes on whatever
# key it names. So a pane can read the release of a key whose press
# has no prompt_toolkit name and never arrived, like a media key. That
# is deliberate. The other way costs a table of the keys that pymux
# can name, and a pane that asked for the event types of a key reads
# a release that matches no press without trouble.
_KEY_RELEASE = object()

_KeyResult = str | Keys | tuple | object

# Reply of the "CSI ? u" flags query.
_FLAGS_REPLY_RE = re.compile(r"^\x1b\[\?(\d+)u$")

# Primary or Secondary device attributes reply.
_DA1_REPLY_RE = re.compile(r"^\x1b\[\?[\d;]*c$")

# Reply of the "CSI 16 t" cell size query.
_CELL_SIZE_REPLY_RE = re.compile(r"^\x1b\[6;\d+;\d+t$")

# Reply of a DECRQM request: "CSI ? <mode> ; <state> $ y".
_MODE_REPLY_RE = re.compile(r"^\x1b\[\?\d+;\d+\$y$")


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


def _apply_modifiers(key: str | Keys, mods: int) -> _KeyResult:
    """
    Apply the modifier bits to a plain key. Returns a `Dropped` when
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
    shift = bool(mods & _SHIFT)
    alt = bool(mods & _ALT)
    ctrl = bool(mods & _CTRL)

    if not isinstance(key, Keys):
        if ctrl:
            ctrl_key = _ctrl_mapping(key)
            if ctrl_key is None:
                return Dropped(DropReason.CTRL_AND_A_CHARACTER)
            key = ctrl_key
        elif shift and key.isalpha():
            key = key.upper()
    elif ctrl or shift:
        modified = _the_modified_form_of(key, ctrl, shift)
        if modified is None:
            return Dropped(DropReason.A_MODIFIER_THIS_KEY_HAS_NO_NAME_FOR)
        key = modified

    if alt:
        return (Keys.Escape, key)
    return key


def _the_modified_form_of(key: Keys, ctrl: bool, shift: bool) -> Keys | None:
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


def _with_alt(key: str | Keys, mods: int) -> _KeyResult:
    """
    Alt on a key, which prompt_toolkit spells as two key presses.

    There is no `Keys` member for alt and no name for it either: an
    escape and the key is what the toolkit binds and what its parser
    feeds. So alt is the last thing applied, after the key it is on has
    its own name.
    """
    return (Keys.Escape, key) if mods & _ALT else key


def the_key_named(base: str, mods: int) -> _KeyResult:
    """
    The prompt_toolkit key that one key with its modifiers is.

    `base` is the key without its modifiers, as `_base_of` writes it: a
    character, or the value of a `Keys` member such as "up" or "f5", or
    one of the four C0 keys by name. `mods` is the bits of
    `pyte.keys.Modifier`.

    Returns a key, a character, a tuple of two for alt, or a `Dropped`
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
        return _with_alt(name_of(base, mods & ~Modifier.ALT), mods)

    if base == "escape":
        return _apply_modifiers(Keys.Escape, mods)

    if base == "enter":
        if mods & _CTRL:
            # ctrl+enter is ctrl+j in the legacy encoding.
            return _with_alt(Keys.ControlJ, mods)
        return _apply_modifiers(Keys.Enter, mods)

    if base == "tab":
        if mods & _SHIFT and not mods & (_CTRL | _ALT):
            return Keys.BackTab
        if mods & _CTRL:
            return _with_alt(Keys.ControlI, mods)
        return _apply_modifiers(Keys.Tab, mods)

    if base == "backspace":
        if mods & _CTRL:
            return _with_alt(Keys.Backspace, mods)
        return _apply_modifiers(Keys.Backspace, mods)

    if len(base) == 1:
        if mods & _CTRL and mods & _SHIFT:
            # Only a terminal that says more than the legacy encoding
            # can tell these apart: ctrl+a and ctrl+shift+a are one
            # control code there. The shift used to be dropped, so a
            # person could bind neither on its own. Lillecarl/pymux#168.
            both = _CTRL_SHIFT_LETTERS.get(base.lower())
            if both is not None:
                return _with_alt(both, mods)
        return _apply_modifiers(base, mods)

    member = A_KEY_BY_ITS_NAME.get(base)
    if member is None:
        return Dropped(DropReason.A_KEY_THAT_WRITES_NOTHING)
    return _apply_modifiers(member, mods)


def _a_reply(prefix: str) -> object | None:
    """
    The kind of terminal reply this sequence is, or None.

    None of these is a key event, and every one of them must be
    consumed all the same, or it reaches a pane as key presses.
    """
    if _FLAGS_REPLY_RE.match(prefix):
        return _FLAGS_REPLY
    if _DA1_REPLY_RE.match(prefix):
        return _DA1_REPLY
    if _CELL_SIZE_REPLY_RE.match(prefix):
        return _CELL_SIZE_REPLY
    if _MODE_REPLY_RE.match(prefix):
        return _MODE_REPLY
    if _STRING_RE.match(prefix):
        return _STRING_REPLY
    return None


def parse_kitty_key(prefix: str) -> _KeyResult | None:
    """
    Parse one complete key sequence into something prompt_toolkit can
    name.

    Returns a key, a character or a tuple of keys (the shapes that the
    prompt_toolkit parser supports), `_KEY_RELEASE` for a key that
    came back up, a `Dropped` for a sequence that names a key pymux
    cannot, and None when `prefix` is not a key sequence at all.

    **The reading is pyte's.** `parse_key_data` knows every mode a
    keyboard can be in, and this file used to carry a second reader
    beside it: its own regex, its own modifier bits, its own keypad.
    Two readers of the same bytes is what Lillecarl/pymux#119 cost 52
    keys, so there is one, and what stays here is the naming: which
    prompt_toolkit key a `KeyEvent` is. Lillecarl/pymux#176.
    """
    reply = _a_reply(prefix)
    if reply is not None:
        return reply

    if not _LOOKS_LIKE_A_KEY_RE.match(prefix):
        return None

    events = [item for item in parse_key_data(prefix) if isinstance(item, KeyEvent)]
    if len(events) != 1:
        # Not one key: an incomplete sequence, or something pyte passes
        # through, or several keys that this parser never feeds at once.
        return None
    return _named(events[0])


def _base_of(event: KeyEvent) -> str | None:
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
            keypad = _KEYPAD.get(event.code)
            if keypad is None:
                return None
            return keypad if isinstance(keypad, str) else str.__str__(keypad)
        return chr(event.code).lower()
    if event.final == "~":
        tilde = _TILDE_KEYS.get(event.code)
        return None if tilde is None else str.__str__(tilde)
    letter = _LETTER_KEYS.get(event.final)
    return None if letter is None else str.__str__(letter)


def _named(event: KeyEvent) -> _KeyResult | None:
    "The prompt_toolkit key that one key event is."
    if event.event == EventType.RELEASE:
        # A key that came back up. It is not a key press, so it does
        # not become one: it carries its own key, which only a pane
        # that asked for the event types reads. See
        # `KittyVt100Parser._call_handler`.
        return _KEY_RELEASE

    key, mods, final, text = event.code, event.mods, event.final, event.text

    if mods & MODIFIERS_WITH_NO_MEMBER:
        # super, hyper or meta, which get a built name. It comes first
        # because it reads the unshifted key, so none of the three
        # readings below may take the key away from it.
        base = _base_of(event)
        if base is None:
            return Dropped(DropReason.A_KEY_THAT_WRITES_NOTHING)
        return the_key_named(base, mods)

    # Three things that only a key off the wire can be, and that a name
    # therefore never says. Each one is read here, before the base and
    # the bits go to the one translation.
    if final == "u":
        # The keypad, which carries its keys in the private use area. A
        # plain press is the key it prints; with ctrl or alt it has no
        # prompt_toolkit form at all.
        if key in _KEYPAD:
            keypad_key = _KEYPAD[key]
            if not isinstance(keypad_key, str):
                return _apply_modifiers(keypad_key, mods)
            if mods & (_CTRL | _ALT):
                return Dropped(DropReason.KEYPAD_WITH_A_MODIFIER)
            return keypad_key

        if key >= FIRST_FUNCTIONAL_KEY:
            # Other private use area keys (lock keys, media keys, ...)
            # have no prompt_toolkit representation. Drop them.
            return Dropped(DropReason.A_KEY_THAT_WRITES_NOTHING)

        # The text the terminal reported for this key, which accounts
        # for the shift modifier and the keyboard layout. Nothing else
        # knows the layout, so this wins over the base.
        #
        # Not for ctrl, which has a control code and not text.
        if text and not mods & _CTRL:
            return _with_alt(text, mods)

    elif final == "~":
        if _TILDE_KEYS.get(key) is None:
            return Dropped(DropReason.A_TILDE_KEY_WITH_NO_NAME)

    elif _LETTER_KEYS.get(final) is None or key != 1:
        # The letter form names ten keys, and carries the number one
        # and nothing else.
        return Dropped(DropReason.A_LETTER_KEY_WITH_NO_NAME)

    base = _base_of(event)
    if base is None:
        return Dropped(DropReason.A_KEY_THAT_WRITES_NOTHING)
    return the_key_named(base, mods)


def _patch_prefix_cache() -> None:
    """
    Teach prompt_toolkit's "is prefix of a longer match" cache about
    sequences with variable parameters, so that incomplete kitty key
    sequences are buffered instead of being decomposed character by
    character. (Same mechanism that prompt_toolkit uses for CPR and
    mouse responses.)
    """
    if getattr(_IsPrefixOfLongerMatchCache, "_kitty_patched", False):
        return

    original = _IsPrefixOfLongerMatchCache.__missing__

    def __missing__(self, prefix: str) -> bool:
        if _KITTY_PREFIX_RE.match(prefix):
            self[prefix] = True
            return True
        if len(prefix) <= MAX_STRING_LENGTH and _STRING_PREFIX_RE.match(prefix):
            # Don't cache: the cache would grow with every payload.
            return True
        return original(self, prefix)

    _IsPrefixOfLongerMatchCache.__missing__ = __missing__
    _IsPrefixOfLongerMatchCache._kitty_patched = True


_patch_prefix_cache()


class KittyVt100Parser(Vt100Parser):
    """
    Vt100 parser that also decodes the key encoding of the kitty
    keyboard protocol.

    :param feed_key_callback: Called for every key press.
    :param reply_callback: Called with the raw sequence for terminal
        replies: keyboard flags replies, device attributes replies,
        cell size reports, and APC, DCS and OSC string sequences. Used
        for protocol support detection. A reply that nothing asked for
        is consumed all the same, so that it cannot reach a pane as key
        presses.
    """

    def __init__(
        self,
        feed_key_callback,
        reply_callback=None,
        speaks_the_protocol=None,
    ) -> None:
        self.reply_callback = reply_callback
        # Whether the terminal counts the modifiers the way the
        # protocol does. Asked when a key arrives and not once at the
        # start, because the detection answers after this is built.
        # None means nobody knows, which reads as no.
        # Lillecarl/pymux#182.
        self.speaks_the_protocol = speaks_the_protocol
        # Whether the key being handled arrived as several: alt and a
        # key is one such. See `_call_handler`.
        self._one_key_of_several = False
        # The sequences already written to the log. A key that is held
        # down repeats, and one line per repeat is a log nobody reads.
        self._already_said: set[str] = set()
        super().__init__(feed_key_callback)

    def _say_it_was_dropped(self, dropped: Dropped, sequence: str) -> None:
        """
        Write to the log that a key went nowhere, and why.

        A key that does nothing and leaves no trace gives a person no
        way to find out what happened. Once per sequence: a key that
        is held down repeats. Lillecarl/pymux#167.
        """
        if sequence in self._already_said:
            return
        if len(self._already_said) < MAX_KEYS_TO_REMEMBER:
            self._already_said.add(sequence)
        logger.debug("No name here for the key %r: %s.", sequence, dropped.reason)

    def _counts_the_modifiers_of_the_protocol(self) -> bool:
        """
        Whether this terminal numbers the modifiers the protocol's way.

        A terminal that speaks the protocol does. One that nobody has
        asked, or that answered no, is read the older way, where the
        fourth modifier is meta and there are no more after it.

        Never raises. It runs on every key, and a keyboard may not stop
        for a question about itself.
        """
        if self.speaks_the_protocol is None:
            return False
        try:
            return bool(self.speaks_the_protocol())
        except Exception:
            logger.exception("Asking what the terminal reports failed.")
            return False

    def _get_match(self, prefix: str) -> Keys | tuple | object | None:
        # A modifier above ctrl is read here first, but only from a
        # terminal that counts them the way the protocol does. xterm
        # has four and the fourth is meta; the protocol has eight and
        # the fourth is super. So "CSI 1;9A" is meta+Up to one and
        # super+Up to the other, and prompt_toolkit's table reads it as
        # alt+Up, which is a third answer again.
        #
        # The detection already knows which terminal this is, so the
        # question is asked rather than guessed. Lillecarl/pymux#182.
        if self._counts_the_modifiers_of_the_protocol():
            modifier = _CARRIES_A_HIGH_MODIFIER_RE.match(prefix)
            if modifier is not None and int(modifier.group(1)) > _CTRL_ALT_SHIFT:
                named = parse_kitty_key(prefix)
                if named is not None:
                    return named

        # prompt_toolkit's own table otherwise: it knows richer
        # variants (like shift+arrow) for the sequences that it covers.
        result = super()._get_match(prefix)
        if result is not None:
            return result
        return parse_kitty_key(prefix)

    def _call_handler(self, key: str | Keys | tuple, insert_text: str) -> None:
        if isinstance(key, tuple):
            # A key that arrives as several, such as alt and a letter.
            # The escape in one of those is not the Escape key, so it
            # must not end the buffer. See below.
            self._one_key_of_several = True
            try:
                super()._call_handler(key, insert_text)
            finally:
                self._one_key_of_several = False
            return
        if isinstance(key, Dropped):
            self._say_it_was_dropped(key, insert_text)
            return
        if key in (
            _FLAGS_REPLY,
            _DA1_REPLY,
            _STRING_REPLY,
            _CELL_SIZE_REPLY,
            _MODE_REPLY,
        ):
            if self.reply_callback is not None:
                self.reply_callback(insert_text)
            return
        if key is _KEY_RELEASE:
            # A release keeps the sequence that the terminal sent, and
            # takes the name of a release. A pane that asked for the
            # event types of a key gets the sequence; nothing else
            # binds that key, so nothing else sees it. Giving it the
            # name of the key instead would run the binding of that
            # key a second time.
            super()._call_handler(Keys.KeyRelease, insert_text)
            return
        super()._call_handler(key, insert_text)
        if (
            key is Keys.Escape
            and not self._one_key_of_several
            and insert_text.startswith(CSI)
        ):
            # The outer terminal spelled this Escape out as "CSI 27 u",
            # so it is the Escape key and not the first byte of a
            # sequence. Say so, by ending the key buffer here.
            #
            # Without it a person waits. A binding may start with
            # escape -- "M-Up" and "M-1" are two that pymux gives a
            # person -- so the key processor holds a bare Escape for
            # `timeoutlen`, which is one second, in case a second key
            # completes one of them. That is why the command line took
            # two presses of Escape to close: the second press is what
            # told the processor that no such key was coming.
            #
            # A terminal that disambiguates writes alt and a key as one
            # sequence of its own, so a bare Escape can complete
            # nothing, and nothing is lost by saying it now.
            # Lillecarl/pymux#164.
            self.feed_key_callback(_Flush)
