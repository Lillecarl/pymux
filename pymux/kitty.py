"""
Kitty keyboard protocol support.

Decodes the CSI u key encoding of the kitty keyboard protocol into
prompt_toolkit key presses.

The outer terminal sends these sequences when the client enabled the
protocol, and for key combinations that have no legacy encoding (like
ctrl+enter) even when the protocol is not enabled. prompt_toolkit's
input parser does not understand them: without this translation the
sequence arrives garbled, one key press per character.

The parser below extends prompt_toolkit's `Vt100Parser`. Sequences that
have no prompt_toolkit representation (key release events, lock keys)
are consumed silently.
"""
import re
from typing import Optional, Union

from prompt_toolkit.input.vt100_parser import (
    Vt100Parser,
    _IsPrefixOfLongerMatchCache,
)
from prompt_toolkit.keys import Keys

__all__ = ["KittyVt100Parser", "parse_kitty_key"]


# Modifier bits of the protocol. (The encoded value is one plus the sum
# of the set bits.)
_SHIFT = 1
_ALT = 2
_CTRL = 4

# Key release event type.
_EVENT_RELEASE = 3

# A complete key event: "CSI number ; modifier u" for text keys,
# "CSI 1 ; modifier [ABCDEFHPQS]" for keys with a legacy CSI encoding,
# and "CSI number ; modifier ~" for keys with a legacy tilde encoding.
# Sub-parameters carry alternate key codes and the event type; a third
# parameter carries the text as code points.
_KITTY_KEY_RE = re.compile(
    r"""
    \x1b\[
    (?P<key>\d+)(?::[\d:]*)?              # key code, optional sub-parameters
    (?:;(?P<mods>\d*)(?::(?P<event>\d+))?)?  # modifiers, optional event type
    (?:;(?P<text>[\d:]*))?                # text as code points
    (?P<final>[u~ABCDEFHPQS])             # final byte
    \Z
    """,
    re.VERBOSE,
)

# A prefix that could still become a kitty key sequence (or any other
# CSI sequence with variable parameters).
_KITTY_PREFIX_RE = re.compile(r"^\x1b\[[0-9;:<=>?]*$")

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

# Sentinel returned for complete sequences that must be consumed without
# producing a key press. (Release events, lock keys, media keys.)
_DROP = object()

_KeyResult = Union[str, Keys, tuple, object]


def _ctrl_mapping(char: str) -> Optional[Keys]:
    "Legacy ctrl+<char> mapping."
    lower = char.lower()
    if "a" <= lower <= "z":
        return getattr(Keys, "Control%s" % lower.upper())
    return _CTRL_KEYS.get(char)


def _apply_modifiers(
    key: Union[str, Keys], mods: int
) -> Optional[_KeyResult]:
    """
    Apply the modifier bits to a plain key. Returns None when the
    combination has no prompt_toolkit representation.
    """
    shift = bool(mods & _SHIFT)
    alt = bool(mods & _ALT)
    ctrl = bool(mods & _CTRL)

    if isinstance(key, str):
        if ctrl:
            ctrl_key = _ctrl_mapping(key)
            if ctrl_key is None:
                return None
            key = ctrl_key
        elif shift and key.isalpha():
            key = key.upper()
    else:
        # Functional key.
        if ctrl:
            ctrl_key = _CTRL_FUNCTIONAL.get(key)
            if ctrl_key is None:
                return None
            key = ctrl_key

    if alt:
        return (Keys.Escape, key)
    return key


# ctrl+<functional key> variants that prompt_toolkit knows.
_CTRL_FUNCTIONAL = {
    Keys.Left: Keys.ControlLeft,
    Keys.Right: Keys.ControlRight,
    Keys.Up: Keys.ControlUp,
    Keys.Down: Keys.ControlDown,
    Keys.Home: Keys.ControlHome,
    Keys.End: Keys.ControlEnd,
    Keys.Insert: Keys.ControlInsert,
    Keys.Delete: Keys.ControlDelete,
    Keys.PageUp: Keys.ControlPageUp,
    Keys.PageDown: Keys.ControlPageDown,
    Keys.F1: Keys.ControlF1,
    Keys.F2: Keys.ControlF2,
    Keys.F3: Keys.ControlF3,
    Keys.F4: Keys.ControlF4,
    Keys.F5: Keys.ControlF5,
    Keys.F6: Keys.ControlF6,
    Keys.F7: Keys.ControlF7,
    Keys.F8: Keys.ControlF8,
    Keys.F9: Keys.ControlF9,
    Keys.F10: Keys.ControlF10,
    Keys.F11: Keys.ControlF11,
    Keys.F12: Keys.ControlF12,
}


def parse_kitty_key(prefix: str) -> Optional[_KeyResult]:
    """
    Parse a complete kitty key sequence. Returns a key, a character or a
    tuple of keys (the shapes that the prompt_toolkit parser supports),
    `_DROP` for sequences to consume silently, and None when `prefix` is
    not a kitty key sequence.
    """
    match = _KITTY_KEY_RE.match(prefix)
    if match is None:
        return None

    if match.group("event") == str(_EVENT_RELEASE):
        return _DROP

    key = int(match.group("key"))
    mods = int(match.group("mods") or 1) - 1
    final = match.group("final")
    text = match.group("text") or ""

    # Enter, Tab and Backspace are reported with their C0 code points.
    if final == "u":
        if key == 27:
            return _apply_modifiers(Keys.Escape, mods)
        if key == 13:
            if mods & _CTRL:
                # ctrl+enter is ctrl+j in the legacy encoding.
                base: _KeyResult = Keys.ControlJ
                return (Keys.Escape, base) if mods & _ALT else base
            return _apply_modifiers(Keys.Enter, mods)
        if key == 9:
            if mods & _CTRL:
                base = Keys.ControlI
                return (Keys.Escape, base) if mods & _ALT else base
            return _apply_modifiers(Keys.Tab, mods)
        if key == 127:
            if mods & _CTRL:
                base = Keys.Backspace
                return (Keys.Escape, base) if mods & _ALT else base
            return _apply_modifiers(Keys.Backspace, mods)

        # Keypad keys.
        if key in _KEYPAD:
            keypad_key = _KEYPAD[key]
            if not isinstance(keypad_key, str):
                return _apply_modifiers(keypad_key, mods)
            if mods & (_CTRL | _ALT):
                return _DROP
            return keypad_key

        if key >= 57344:
            # Other private use area keys (lock keys, media keys, ...)
            # have no prompt_toolkit representation. Drop them.
            return _DROP

        # Text key.
        char = chr(key)
        if mods & _CTRL:
            ctrl_key = _ctrl_mapping(char)
            if ctrl_key is None:
                return _DROP
            return (Keys.Escape, ctrl_key) if mods & _ALT else ctrl_key

        # Use the reported text when present. (It accounts for the
        # shift modifier and the keyboard layout.)
        if text and not (mods & _ALT):
            return "".join(chr(int(code)) for code in text.split(":") if code)
        if text and mods & _ALT:
            plain = "".join(chr(int(code)) for code in text.split(":") if code)
            return (Keys.Escape, plain)

        if mods & _SHIFT and char.isalpha():
            char = char.upper()
        return (Keys.Escape, char) if mods & _ALT else char

    if final == "~":
        tilde_key = _TILDE_KEYS.get(key)
        if tilde_key is None:
            return _DROP
        result = _apply_modifiers(tilde_key, mods)
        return result if result is not None else _DROP

    # Letter form. (The number is always 1.)
    letter_key = _LETTER_KEYS.get(final)
    if key != 1 or letter_key is None:
        return _DROP
    result = _apply_modifiers(letter_key, mods)
    return result if result is not None else _DROP


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
        return original(self, prefix)

    _IsPrefixOfLongerMatchCache.__missing__ = __missing__
    _IsPrefixOfLongerMatchCache._kitty_patched = True


_patch_prefix_cache()


class KittyVt100Parser(Vt100Parser):
    """
    Vt100 parser that also decodes the key encoding of the kitty
    keyboard protocol.
    """

    def _get_match(self, prefix: str) -> Optional[Union[Keys, tuple, object]]:
        # prompt_toolkit's own table first: it knows richer variants
        # (like shift+arrow) for the sequences that it covers.
        result = super()._get_match(prefix)
        if result is not None:
            return result
        return parse_kitty_key(prefix)

    def _call_handler(
        self, key: Union[str, Keys, tuple], insert_text: str
    ) -> None:
        if key is _DROP:
            return
        super()._call_handler(key, insert_text)
