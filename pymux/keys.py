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
from prompt_toolkit.keys import Keys

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

__all__ = ["DropReason", "Dropped", "KittyVt100Parser", "parse_kitty_key"]


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
    A_LETTER_KEY_WITH_NO_NAME = (
        "a key of the letter form that pymux cannot name"
    )
    A_MODIFIER_THIS_KEY_HAS_NO_NAME_FOR = (
        "a modifier that this key has no name for"
    )

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
    all of them, and `_CTRL_FUNCTIONAL` was never read. ctrl+Up asked
    for `Keys.ControlUP`, which is not a name, and prompt_toolkit's own
    table is the only reason nothing raised: it matches every sequence
    that would have come here.
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
    else:
        # Functional key.
        if ctrl:
            ctrl_key = _CTRL_FUNCTIONAL.get(key)
            if ctrl_key is None:
                return Dropped(
                    DropReason.A_MODIFIER_THIS_KEY_HAS_NO_NAME_FOR
                )
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

    events = [
        item
        for item in parse_key_data(prefix)
        if isinstance(item, KeyEvent)
    ]
    if len(events) != 1:
        # Not one key: an incomplete sequence, or something pyte passes
        # through, or several keys that this parser never feeds at once.
        return None
    return _named(events[0])


def _named(event: KeyEvent) -> _KeyResult | None:
    "The prompt_toolkit key that one key event is."
    if event.event == EventType.RELEASE:
        # A key that came back up. It is not a key press, so it does
        # not become one: it carries its own key, which only a pane
        # that asked for the event types reads. See
        # `KittyVt100Parser._call_handler`.
        return _KEY_RELEASE

    key, mods, final, text = event.code, event.mods, event.final, event.text

    if final == "u":
        # Enter, Tab and Backspace carry their C0 code points.
        if key == KeyCode.ESCAPE:
            return _apply_modifiers(Keys.Escape, mods)
        if key == KeyCode.ENTER:
            if mods & _CTRL:
                # ctrl+enter is ctrl+j in the legacy encoding.
                base: _KeyResult = Keys.ControlJ
                return (Keys.Escape, base) if mods & _ALT else base
            return _apply_modifiers(Keys.Enter, mods)
        if key == KeyCode.TAB:
            if mods & _SHIFT and not mods & (_CTRL | _ALT):
                return Keys.BackTab
            if mods & _CTRL:
                base = Keys.ControlI
                return (Keys.Escape, base) if mods & _ALT else base
            return _apply_modifiers(Keys.Tab, mods)
        if key == KeyCode.BACKSPACE:
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
                return Dropped(DropReason.KEYPAD_WITH_A_MODIFIER)
            return keypad_key

        if key >= FIRST_FUNCTIONAL_KEY:
            # Other private use area keys (lock keys, media keys, ...)
            # have no prompt_toolkit representation. Drop them.
            return Dropped(DropReason.A_KEY_THAT_WRITES_NOTHING)

        # Text key.
        char = chr(key)
        if mods & _CTRL:
            ctrl_key = _ctrl_mapping(char)
            if ctrl_key is None:
                return Dropped(DropReason.CTRL_AND_A_CHARACTER)
            return (Keys.Escape, ctrl_key) if mods & _ALT else ctrl_key

        # Use the reported text when present. (It accounts for the
        # shift modifier and the keyboard layout.)
        if text:
            return (Keys.Escape, text) if mods & _ALT else text

        if mods & _SHIFT and char.isalpha():
            char = char.upper()
        return (Keys.Escape, char) if mods & _ALT else char

    if final == "~":
        tilde_key = _TILDE_KEYS.get(key)
        if tilde_key is None:
            return Dropped(DropReason.A_TILDE_KEY_WITH_NO_NAME)
        return _apply_modifiers(tilde_key, mods)

    # Letter form. (The number is always 1.)
    letter_key = _LETTER_KEYS.get(final)
    if key != 1 or letter_key is None:
        return Dropped(DropReason.A_LETTER_KEY_WITH_NO_NAME)
    return _apply_modifiers(letter_key, mods)


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
        if (
            len(prefix) <= MAX_STRING_LENGTH
            and _STRING_PREFIX_RE.match(prefix)
        ):
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

    def __init__(self, feed_key_callback, reply_callback=None) -> None:
        self.reply_callback = reply_callback
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
        logger.debug(
            "No name here for the key %r: %s.", sequence, dropped.reason
        )

    def _get_match(self, prefix: str) -> Keys | tuple | object | None:
        # prompt_toolkit's own table first: it knows richer variants
        # (like shift+arrow) for the sequences that it covers.
        result = super()._get_match(prefix)
        if result is not None:
            return result
        return parse_kitty_key(prefix)

    def _call_handler(
        self, key: str | Keys | tuple, insert_text: str
    ) -> None:
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
