"""
The names tmux gives a key, and the bytes that key sends.

Two things read this. `bind-key` names a key, so a binding needs the
prompt_toolkit key that a press of it arrives as. And `send-keys` types
into a pane, so it needs the bytes the pane would have read if a person
had pressed the key.

**A key press does not come through here.** ptterm hands the pane the
bytes the terminal of the person really sent, and `Screen.encode_key`
re-spells them for the modes the pane turned on. So this table is the
one place a key becomes bytes without a keyboard, and the bytes it
gives have to be the ones a keyboard would have given.

They were not, for 52 of them. ptterm carried a second copy of this
file, built the other way round, and `send-keys Up` sent the
application cursor form to a pane that never asked for it while a press
of the same key sent the plain one. One table now, and
`tests/test_send_keys.py` reads the bytes. Lillecarl/pymux#119.
"""

from typing import Dict, Tuple

from prompt_toolkit.input.vt100_parser import ANSI_SEQUENCES
from prompt_toolkit.keys import KeyName, Keys
from pyte.keys import Modifier

from .keys import A_KEY_BY_ITS_NAME, MODIFIERS_WITH_NO_MEMBER, name_of

__all__ = [
    "pymux_key_to_prompt_toolkit_key_sequence",
    "prompt_toolkit_key_to_vt100_key",
    "PYMUX_TO_PROMPT_TOOLKIT_KEYS",
]


#: What a person writes for each modifier, and the bit it means.
#:
#: tmux has no spelling for super, hyper or meta, so there is no muscle
#: memory to keep here and the names are written out. "S-" and "C-"
#: keep the tmux meaning they already have.
THE_MODIFIERS_A_PERSON_WRITES = (
    ("s-", Modifier.SHIFT),
    ("c-", Modifier.CTRL),
    ("super-", Modifier.SUPER),
    ("hyper-", Modifier.HYPER),
    ("meta-", Modifier.META),
)


def _a_built_name(key: str) -> KeyName | None:
    """
    The key that a name with super, hyper or meta in it means.

    None for anything else, and the tables below answer instead. Only
    these three need building: everything the legacy encoding can
    carry has a `Keys` member already.

    The order the modifiers are written in does not matter to a
    person, and it does to the name, so the name is rebuilt from what
    was found rather than from what was typed.
    """
    rest = key.lower()
    mods = 0
    found = True
    while found:
        found = False
        for spelling, modifier in THE_MODIFIERS_A_PERSON_WRITES:
            if rest.startswith(spelling) and len(rest) > len(spelling):
                mods |= modifier
                rest = rest[len(spelling) :]
                found = True
                break

    if not mods & MODIFIERS_WITH_NO_MEMBER:
        return None
    return name_of(rest, mods)


def pymux_key_to_prompt_toolkit_key_sequence(key):
    """
    Turn a pymux description of a key. E.g.  "C-a" or "M-x" into a
    prompt-toolkit key sequence.

    Raises `ValueError` if the key is not known.
    """
    # super, hyper and meta, which no `Keys` member names and no list
    # could. The name is built the same way the reader builds it, so
    # the two meet in the middle. Lillecarl/pymux#181.
    built = _a_built_name(key)
    if built is not None:
        return (built,)

    # Make the c-, m- and s- prefixes case insensitive.
    #
    # The shift one has to come first, or "C-S-a" loses its S to the
    # branch that only knows "C-". A person writes a key the way it
    # reads, and "c-s-a" has to name what "C-S-a" names.
    if key.lower().startswith("c-s-"):
        key = "C-S-" + key[4:]
    elif key.lower().startswith("m-c-"):
        key = "M-C-" + key[4:]
    elif key.lower().startswith("c-"):
        key = "C-" + key[2:]
    elif key.lower().startswith("m-"):
        key = "M-" + key[2:]

    # Lookup key.
    try:
        return PYMUX_TO_PROMPT_TOOLKIT_KEYS[key]
    except KeyError:
        if len(key) == 1:
            return (key,)

        # **Alt and one character is escape and that character**, for
        # any character and not only the ones the table writes out.
        #
        # The table names "M-a" to "M-z" and "M-0" to "M-9" one at a
        # time, which left out every other single character: "M-J",
        # which tmux binds, answered "Invalid key". A list of the ones
        # somebody thought of is not the rule, and the rule is one
        # line. Lillecarl/pymux#204.
        #
        # The table keeps those entries because it is also the list
        # that completes a key (`commands/completer.py`,
        # `options.py`). Writing out "M-A" to "M-Z" and every
        # punctuation mark would make that list unreadable to save
        # nothing.
        if len(key) == 3 and key.startswith("M-"):
            return (Keys.Escape, key[2])

        raise ValueError("Unknown key: %r" % (key,))


def _keys_to_data() -> Dict[Keys, str]:
    """
    The bytes of each prompt_toolkit key, out of the table that reads
    them.

    **The first sequence of a key wins, and a key spelled as a tuple is
    left out.** `ANSI_SEQUENCES` maps bytes to a key, and several runs
    of bytes reach the same key: "\\r" and "\\x1b[27;6;13~" are both
    `ControlM`. Inverting the whole thing keeps the last, so Enter went
    out as the modified form nobody pressed. A tuple is a key that
    arrives as two, such as escape and a letter, and it is not one key
    at all.
    """
    result: Dict[Keys, str] = {}
    for vt100_data, key in ANSI_SEQUENCES.items():
        if not isinstance(key, tuple) and key not in result:
            result[key] = vt100_data
    return result


#: What ctrl and shift on a letter send to a pane.
#:
#: The legacy encoding has no form of its own for these: ctrl+a and
#: ctrl+shift+a are the same control code. So `send-keys C-S-a` sends
#: what a legacy keyboard would have sent, which is ctrl+a.
#:
#: `ANSI_SEQUENCES` cannot answer, because nothing in it produces one
#: of these keys: they reach pymux through the reader in `keys.py`,
#: out of a terminal that speaks a newer encoding.
#: Lillecarl/pymux#168.
_CTRL_SHIFT_TO_VT100 = {
    getattr(Keys, "ControlShift%s" % chr(ord("A") + i)): chr(i + 1) for i in range(26)
}

_PROMPT_TOOLKIT_KEY_TO_VT100 = {**_keys_to_data(), **_CTRL_SHIFT_TO_VT100}


def prompt_toolkit_key_to_vt100_key(key: str, application_mode: bool = False) -> str:
    """
    Turn a prompt_toolkit key, such as `Keys.ControlB`, into the bytes
    a keyboard sends for it, such as "\\x1b[A".

    `application_mode` is DECCKM, which a program turns on to get the
    SS3 form of an arrow. Only the four arrows have one, and a program
    that never asked for it must not be given it.
    """
    application_mode_keys: Dict[str, str] = {
        Keys.Up: "\x1bOA",
        Keys.Left: "\x1bOD",
        Keys.Right: "\x1bOC",
        Keys.Down: "\x1bOB",
    }

    if application_mode:
        try:
            return application_mode_keys[key]
        except KeyError:
            pass

    if key in _PROMPT_TOOLKIT_KEY_TO_VT100:
        return _PROMPT_TOOLKIT_KEY_TO_VT100[key]

    legacy = _a_built_name_as_legacy_bytes(key)
    if legacy is not None:
        return legacy

    return key


def _a_built_name_as_legacy_bytes(key: str) -> str | None:
    """
    What a keyboard would have sent for a key named with super, hyper
    or meta, and None when the name is not one of those.

    The legacy encoding cannot carry those three at all, so what is
    left is the key with the modifiers it can carry. `send-keys
    Super-a` types an "a", which is what pressing that key on a
    keyboard the pane can hear would have done.

    This is the same trade `send-keys C-S-a` makes, and the same one
    `pyte.keys` makes for a pane that asked for nothing.
    """
    if not isinstance(key, str) or "-" not in key:
        return None
    mods = 0
    rest = key
    while True:
        for spelling, modifier in THE_MODIFIERS_A_PERSON_WRITES:
            if rest.startswith(spelling) and len(rest) > len(spelling):
                mods |= modifier
                rest = rest[len(spelling) :]
                break
        else:
            break

    if not mods & MODIFIERS_WITH_NO_MEMBER:
        return None
    if mods & Modifier.CTRL and len(rest) == 1:
        return prompt_toolkit_key_to_vt100_key("c-%s" % rest)
    if mods & Modifier.SHIFT and len(rest) == 1:
        return rest.upper()
    if len(rest) == 1:
        return rest
    member = A_KEY_BY_ITS_NAME.get(rest)
    if member is None:
        return None
    return prompt_toolkit_key_to_vt100_key(member)


#: ctrl and shift on a letter, which tmux spells "C-S-a".
#:
#: **A pane cannot be sent one of these.** `send-keys` needs the bytes
#: a keyboard sends, and the legacy encoding has none for this: ctrl+a
#: and ctrl+shift+a are one control code there. So a binding can name
#: the key and `prompt_toolkit_key_to_vt100_key` gives what ctrl alone
#: gives, which is what a legacy keyboard would have sent.
#: Lillecarl/pymux#168.
_CTRL_SHIFT_LETTERS: Dict[str, Tuple[str, ...]] = {
    "C-S-%s" % chr(ord("a") + i): (getattr(Keys, "ControlShift%s" % chr(ord("A") + i)),)
    for i in range(26)
}

PYMUX_TO_PROMPT_TOOLKIT_KEYS: Dict[str, Tuple[str, ...]] = {
    **_CTRL_SHIFT_LETTERS,
    # The comma is what makes this a tuple of one. Without it the value
    # is the string, and a caller that walks the keys of a sequence
    # walks the letters of a word instead.
    "Space": (" ",),
    "C-a": (Keys.ControlA,),
    "C-b": (Keys.ControlB,),
    "C-c": (Keys.ControlC,),
    "C-d": (Keys.ControlD,),
    "C-e": (Keys.ControlE,),
    "C-f": (Keys.ControlF,),
    "C-g": (Keys.ControlG,),
    "C-h": (Keys.ControlH,),
    "C-i": (Keys.ControlI,),
    "C-j": (Keys.ControlJ,),
    "C-k": (Keys.ControlK,),
    "C-l": (Keys.ControlL,),
    "C-m": (Keys.ControlM,),
    "C-n": (Keys.ControlN,),
    "C-o": (Keys.ControlO,),
    "C-p": (Keys.ControlP,),
    "C-q": (Keys.ControlQ,),
    "C-r": (Keys.ControlR,),
    "C-s": (Keys.ControlS,),
    "C-t": (Keys.ControlT,),
    "C-u": (Keys.ControlU,),
    "C-v": (Keys.ControlV,),
    "C-w": (Keys.ControlW,),
    "C-x": (Keys.ControlX,),
    "C-y": (Keys.ControlY,),
    "C-z": (Keys.ControlZ,),
    "C-Left": (Keys.ControlLeft,),
    "C-Right": (Keys.ControlRight,),
    "C-Up": (Keys.ControlUp,),
    "C-Down": (Keys.ControlDown,),
    "C-\\": (Keys.ControlBackslash,),
    "S-Left": (Keys.ShiftLeft,),
    "S-Right": (Keys.ShiftRight,),
    "S-Up": (Keys.ShiftUp,),
    "S-Down": (Keys.ShiftDown,),
    "M-C-a": (
        Keys.Escape,
        Keys.ControlA,
    ),
    "M-C-b": (
        Keys.Escape,
        Keys.ControlB,
    ),
    "M-C-c": (
        Keys.Escape,
        Keys.ControlC,
    ),
    "M-C-d": (
        Keys.Escape,
        Keys.ControlD,
    ),
    "M-C-e": (
        Keys.Escape,
        Keys.ControlE,
    ),
    "M-C-f": (
        Keys.Escape,
        Keys.ControlF,
    ),
    "M-C-g": (
        Keys.Escape,
        Keys.ControlG,
    ),
    "M-C-h": (
        Keys.Escape,
        Keys.ControlH,
    ),
    "M-C-i": (
        Keys.Escape,
        Keys.ControlI,
    ),
    "M-C-j": (
        Keys.Escape,
        Keys.ControlJ,
    ),
    "M-C-k": (
        Keys.Escape,
        Keys.ControlK,
    ),
    "M-C-l": (
        Keys.Escape,
        Keys.ControlL,
    ),
    "M-C-m": (
        Keys.Escape,
        Keys.ControlM,
    ),
    "M-C-n": (
        Keys.Escape,
        Keys.ControlN,
    ),
    "M-C-o": (
        Keys.Escape,
        Keys.ControlO,
    ),
    "M-C-p": (
        Keys.Escape,
        Keys.ControlP,
    ),
    "M-C-q": (
        Keys.Escape,
        Keys.ControlQ,
    ),
    "M-C-r": (
        Keys.Escape,
        Keys.ControlR,
    ),
    "M-C-s": (
        Keys.Escape,
        Keys.ControlS,
    ),
    "M-C-t": (
        Keys.Escape,
        Keys.ControlT,
    ),
    "M-C-u": (
        Keys.Escape,
        Keys.ControlU,
    ),
    "M-C-v": (
        Keys.Escape,
        Keys.ControlV,
    ),
    "M-C-w": (
        Keys.Escape,
        Keys.ControlW,
    ),
    "M-C-x": (
        Keys.Escape,
        Keys.ControlX,
    ),
    "M-C-y": (
        Keys.Escape,
        Keys.ControlY,
    ),
    "M-C-z": (
        Keys.Escape,
        Keys.ControlZ,
    ),
    "M-C-Left": (
        Keys.Escape,
        Keys.ControlLeft,
    ),
    "M-C-Right": (
        Keys.Escape,
        Keys.ControlRight,
    ),
    "M-C-Up": (
        Keys.Escape,
        Keys.ControlUp,
    ),
    "M-C-Down": (
        Keys.Escape,
        Keys.ControlDown,
    ),
    "M-C-\\": (
        Keys.Escape,
        Keys.ControlBackslash,
    ),
    "M-a": (Keys.Escape, "a"),
    "M-b": (Keys.Escape, "b"),
    "M-c": (Keys.Escape, "c"),
    "M-d": (Keys.Escape, "d"),
    "M-e": (Keys.Escape, "e"),
    "M-f": (Keys.Escape, "f"),
    "M-g": (Keys.Escape, "g"),
    "M-h": (Keys.Escape, "h"),
    "M-i": (Keys.Escape, "i"),
    "M-j": (Keys.Escape, "j"),
    "M-k": (Keys.Escape, "k"),
    "M-l": (Keys.Escape, "l"),
    "M-m": (Keys.Escape, "m"),
    "M-n": (Keys.Escape, "n"),
    "M-o": (Keys.Escape, "o"),
    "M-p": (Keys.Escape, "p"),
    "M-q": (Keys.Escape, "q"),
    "M-r": (Keys.Escape, "r"),
    "M-s": (Keys.Escape, "s"),
    "M-t": (Keys.Escape, "t"),
    "M-u": (Keys.Escape, "u"),
    "M-v": (Keys.Escape, "v"),
    "M-w": (Keys.Escape, "w"),
    "M-x": (Keys.Escape, "x"),
    "M-y": (Keys.Escape, "y"),
    "M-z": (Keys.Escape, "z"),
    "M-0": (Keys.Escape, "0"),
    "M-1": (Keys.Escape, "1"),
    "M-2": (Keys.Escape, "2"),
    "M-3": (Keys.Escape, "3"),
    "M-4": (Keys.Escape, "4"),
    "M-5": (Keys.Escape, "5"),
    "M-6": (Keys.Escape, "6"),
    "M-7": (Keys.Escape, "7"),
    "M-8": (Keys.Escape, "8"),
    "M-9": (Keys.Escape, "9"),
    "M-Up": (Keys.Escape, Keys.Up),
    "M-Down": (
        Keys.Escape,
        Keys.Down,
    ),
    "M-Left": (
        Keys.Escape,
        Keys.Left,
    ),
    "M-Right": (
        Keys.Escape,
        Keys.Right,
    ),
    "Left": (Keys.Left,),
    "Right": (Keys.Right,),
    "Up": (Keys.Up,),
    "Down": (Keys.Down,),
    "BSpace": (Keys.Backspace,),
    "BTab": (Keys.BackTab,),
    "DC": (Keys.Delete,),
    "IC": (Keys.Insert,),
    "End": (Keys.End,),
    # Enter is the carriage return, and `ControlJ` is the line feed.
    # A terminal sends "\r" when a person presses Enter, so a binding
    # on it has to be on the key that "\r" arrives as, and `send-keys
    # Enter` has to send "\r" as well. Naming the line feed here made
    # both wrong, and a special case in `prompt_toolkit_key_to_vt100_key`
    # answered "\r" for `ControlJ` to hide it. redis-cli is the program
    # that found that. Lillecarl/pymux#119.
    "Enter": (Keys.ControlM,),
    "Home": (Keys.Home,),
    "Escape": (Keys.Escape,),
    "Tab": (Keys.Tab,),
    "F1": (Keys.F1,),
    "F2": (Keys.F2,),
    "F3": (Keys.F3,),
    "F4": (Keys.F4,),
    "F5": (Keys.F5,),
    "F6": (Keys.F6,),
    "F7": (Keys.F7,),
    "F8": (Keys.F8,),
    "F9": (Keys.F9,),
    "F10": (Keys.F10,),
    "F11": (Keys.F11,),
    "F12": (Keys.F12,),
    "F13": (Keys.F13,),
    "F14": (Keys.F14,),
    "F15": (Keys.F15,),
    "F16": (Keys.F16,),
    "F17": (Keys.F17,),
    "F18": (Keys.F18,),
    "F19": (Keys.F19,),
    "F20": (Keys.F20,),
    "NPage": (Keys.PageDown,),
    "PageDown": (Keys.PageDown,),
    "PgDn": (Keys.PageDown,),
    "PPage": (Keys.PageUp,),
    "PageUp": (Keys.PageUp,),
    "PgUp": (Keys.PageUp,),
}
