"""
The keyboard of the person, as a prompt_toolkit key press.

The outer terminal sends the extended forms when the client asked for
them, and for combinations that have no legacy encoding, such as
ctrl+enter, even when it did not. prompt_toolkit's input parser knows
the legacy encoding and none of the rest: without this the sequence
arrives garbled, one key press per character.

**The reading is pyte's.** `pyte.keys.parse_key_data` knows every mode
a keyboard can be in, and there is no second reader here: two readers
of the same bytes cost 52 wrong keys once (Lillecarl/pymux#119). What
is here is the naming: which prompt_toolkit key a `KeyEvent` is,
which pyte cannot say because a toolkit sits above it. `names.py`
holds the tables.

The parser below extends prompt_toolkit's `Vt100Parser`. A key it
cannot name is written to the log, with the reason, and handed on as
`Keys.UnnamedKey` with the sequence the terminal sent, which the pane
encodes for its own keyboard mode.

A key that came back up is not a key press, so it takes the key of a
release (`Keys.KeyRelease`) and keeps the sequence that the terminal
sent. A pane that asked for the event types of a key is given that
sequence; nothing else binds that key, so nothing else sees it.

The parser also swallows the string sequences that carry the replies of
the terminal queries: APC (`ESC _ ... ST`) for the graphics protocol,
DCS (`ESC P ... ST`) for the colour depth probe, OSC (`ESC ] ... ST`)
for the colour and clipboard answers, and the `CSI ... t` window report
for the cell size. Without this the replies would arrive as bursts of
key presses, and land in whichever pane has the focus. `replies.py`
knows each reply.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, cast, override

from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding.key_processor import _Flush
from prompt_toolkit.keys import Keys

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_processor import KeyPress
from pyte.keys.events import FIRST_FUNCTIONAL_KEY, EventType, KeyEvent
from pyte.keys.parse import parse_key_data
from pyte.sequences.reports import SgrMouseReport

from .names import (
    ALT,
    CTRL,
    KEYPAD,
    LETTER_KEYS,
    MODIFIERS_WITH_NO_MEMBER,
    SHIFT,
    TILDE_KEYS,
    Key,
    Unnamed,
    UnnamedReason,
    apply_modifiers,
    base_of,
    key_named,
    with_alt,
)
from .replies import TERMINAL_REPLY, pixels_to_cells, reply_of

__all__ = ["CSI", "MAX_KEYS_TO_REMEMBER", "MAX_STRING_LENGTH", "KittyVt100Parser", "logger", "parse_kitty_key"]


logger = logging.getLogger(__name__)


#: The bytes that introduce a control sequence. A key that arrives in
#: one is a key the outer terminal spelled out, and not a byte that
#: something else could still continue.
CSI = "\x1b["


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
_CARRIES_A_HIGH_MODIFIER_RE = re.compile(r"^\x1b\[[\d:]*;(\d+)[\d:]*(?:;[\d:]*)?[u~ABCDEFHPQS]$")


#: The largest modifier value the legacy numbering can mean: shift and
#: alt and ctrl together, which is one plus seven.
_CTRL_ALT_SHIFT = 8


#: F3 with shift, alt or ctrl as a legacy terminal spells it, the letter
#: form that is also a cursor report of row 1. Modifier 1 is no
#: modifier, and the parameter of a report of row 1, column 1.
_MODIFIED_F3_RE = re.compile(r"^\x1b\[1;([2-8])R\Z")


# A prefix that could still become a kitty key sequence (or any other
# CSI sequence with variable parameters).
#
# "$" is in there for DECRQM, whose reply is "CSI ? <mode> ; <state> $ y".
# Without it the parser stops buffering at the "$" and hands the rest
# out as key presses.
_KITTY_PREFIX_RE = re.compile(r"^\x1b\[[0-9;:<=>?$]*$")


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


#: Everything the parser may hand on, the reply sentinels included.
_KeyResult = Key | object


def parse_kitty_key(prefix: str) -> _KeyResult | None:
    """
    Parse one complete key sequence into something prompt_toolkit can
    name.

    Returns a key, a character or a tuple of keys (the shapes that the
    prompt_toolkit parser supports), `_KEY_RELEASE` for a key that
    came back up, an `Unnamed` for a sequence that names a key pymux
    cannot, and None when `prefix` is not a key sequence at all.

    **The reading is pyte's.** `parse_key_data` knows every mode a
    keyboard can be in, and there is no second reader here with its own
    regex, modifier bits and keypad: two readers of the same bytes cost
    52 keys in Lillecarl/pymux#119. What is here is the naming: which
    prompt_toolkit key a `KeyEvent` is. Lillecarl/pymux#176.
    """
    reply = reply_of(prefix)
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
        base = base_of(event)
        if base is None:
            return Unnamed(UnnamedReason.KEY_THAT_WRITES_NOTHING)
        return key_named(base, mods)

    # Three things that only a key off the wire can be, and that a name
    # therefore never says. Each one is read here, before the base and
    # the bits go to the one translation.
    if final == "u":
        # The keypad, which carries its keys in the private use area. A
        # plain press is the key it prints; with ctrl or alt it has no
        # prompt_toolkit form at all.
        if key in KEYPAD:
            keypad_key = KEYPAD[key]
            if not isinstance(keypad_key, str):
                return apply_modifiers(keypad_key, mods)
            if mods & (CTRL | ALT):
                return Unnamed(UnnamedReason.KEYPAD_WITH_A_MODIFIER)
            return keypad_key

        if key >= FIRST_FUNCTIONAL_KEY:
            # Other private use area keys (lock keys, media keys, ...)
            # have no prompt_toolkit representation.
            return Unnamed(UnnamedReason.KEY_THAT_WRITES_NOTHING)

        # The text the terminal reported for this key, which accounts
        # for the shift modifier and the keyboard layout. Nothing else
        # knows the layout, so this wins over the base.
        #
        # Not for ctrl, which has a control code and not text.
        if text and not mods & CTRL:
            return with_alt(text, mods)

        # The shifted key, which the layout gives as well: ':' for
        # shift+; on a US layout. kitty sends it with no text in a
        # keyboard mode that passes every key on.
        shifted = event.alternates[0] if event.alternates else None
        if shifted is not None and mods & SHIFT and not mods & CTRL:
            return with_alt(chr(shifted), mods)

    elif final == "~":
        if TILDE_KEYS.get(key) is None:
            return Unnamed(UnnamedReason.TILDE_KEY_WITH_NO_NAME)

    elif LETTER_KEYS.get(final) is None or key != 1:
        # The letter form names ten keys, and carries the number one
        # and nothing else.
        return Unnamed(UnnamedReason.LETTER_KEY_WITH_NO_NAME)

    base = base_of(event)
    if base is None:
        return Unnamed(UnnamedReason.KEY_THAT_WRITES_NOTHING)
    return key_named(base, mods)


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
        feed_key_callback: Callable[[KeyPress], None],
        reply_callback: Callable[[str], None] | None = None,
        speaks_protocol: Callable[[], bool | None] | None = None,
        pixel_cell: Callable[[], tuple[int, int, int] | None] | None = None,
        pointer_moved: Callable[[], None] | None = None,
    ) -> None:
        self.reply_callback = reply_callback
        # The cell size of a terminal that reports the mouse in pixels
        # (mode 1016), or None while it reports cells. Asked at each
        # report, because the mode turns on after the detection.
        # Lillecarl/pymux#648.
        self.pixel_cell = pixel_cell
        #: Where in its cell the last mouse report was, as fractions.
        self.pointer_fraction = (0.5, 0.5)
        #: The cell of the last mouse report, column and row from 0.
        self.pointer_cell: tuple[int, int] | None = None
        #: Called after each mouse report, before a handler reads it.
        self.pointer_moved = pointer_moved
        # Whether the terminal counts the modifiers the way the
        # protocol does. Asked when a key arrives and not once at the
        # start, because the detection answers after this is built.
        # None means nobody knows, which reads as no.
        # Lillecarl/pymux#182.
        self.speaks_protocol = speaks_protocol
        # Whether the key being handled arrived as several: alt and a
        # key is one such. See `_call_handler`.
        self._one_key_of_several = False
        # The sequences already written to the log. A key that is held
        # down repeats, and one line per repeat is a log nobody reads.
        self._already_said: set[str] = set()
        super().__init__(feed_key_callback)

    def _say_it_has_no_name(self, unnamed: Unnamed, sequence: str) -> None:
        """
        Write to the log that a key has no name here, and why.

        No binding of pymux's takes such a key, so this is how a person
        finds out why it did nothing outside a pane. Once per sequence:
        a key that is held down repeats. Lillecarl/pymux#167.
        """
        if sequence in self._already_said:
            return
        if len(self._already_said) < MAX_KEYS_TO_REMEMBER:
            self._already_said.add(sequence)
        logger.debug("No name here for the key %r: %s.", sequence, unnamed.reason)

    def _counts_protocol_modifiers(self) -> bool:
        """
        Whether this terminal numbers the modifiers the protocol's way.

        A terminal that speaks the protocol does. One that nobody has
        asked, or that answered no, is read the older way, where the
        fourth modifier is meta and there are no more after it.

        Never raises. It runs on every key, and a keyboard may not stop
        for a question about itself.
        """
        if self.speaks_protocol is None:
            return False
        try:
            return bool(self.speaks_protocol())
        except Exception:
            logger.exception("Asking what the terminal reports failed.")
            return False

    @override
    def _is_prefix_of_longer_match(self, prefix: str) -> bool:
        """
        Wait for the rest of a kitty key sequence or a string reply,
        instead of handing it out a character at a time. These two have
        variable parameters, which prompt_toolkit's table cannot list.
        Lillecarl/pymux#697.
        """
        if _KITTY_PREFIX_RE.match(prefix):
            return True
        if len(prefix) <= MAX_STRING_LENGTH and _STRING_PREFIX_RE.match(prefix):
            return True
        return super()._is_prefix_of_longer_match(prefix)

    @override
    def _get_match(self, prefix: str) -> Keys | tuple[Keys, ...] | None:
        # A modifier above ctrl is read here first, but only from a
        # terminal that counts them the way the protocol does. xterm
        # has four and the fourth is meta; the protocol has eight and
        # the fourth is super. So "CSI 1;9A" is meta+Up to one and
        # super+Up to the other, and prompt_toolkit's table reads it as
        # alt+Up, which is a third answer again.
        #
        # The detection already knows which terminal this is, so the
        # question is asked rather than guessed. Lillecarl/pymux#182.
        if self._counts_protocol_modifiers():
            modifier = _CARRIES_A_HIGH_MODIFIER_RE.match(prefix)
            if modifier is not None and int(modifier.group(1)) > _CTRL_ALT_SHIFT:
                named = parse_kitty_key(prefix)
                if named is not None:
                    return cast("Keys | tuple[Keys, ...]", named)

        # ctrl+F3 is "CSI 1;5R" in every legacy terminal, which is also a
        # cursor report of row 1. The reports the server asks for are
        # `EMOJI_WIDTH_PROBE`'s, on row 2, and `CLUSTER_WIDTH_PROBE`'s,
        # on rows 3 to 5, so row 1 is the key. A probe that asks on row
        # 1 would be read as a key press here. Lillecarl/pymux#244.
        f3 = _MODIFIED_F3_RE.match(prefix)
        if f3 is not None:
            return cast("Keys | tuple[Keys, ...]", key_named(str.__str__(Keys.F3), int(f3.group(1)) - 1))

        # prompt_toolkit's own table otherwise: it knows richer
        # variants (like shift+arrow) for the sequences that it covers.
        result = super()._get_match(prefix)
        if result is not None:
            return result
        return cast("Keys | tuple[Keys, ...] | None", parse_kitty_key(prefix))

    @override
    def _call_handler(self, key: str | Keys | tuple[Keys, ...], insert_text: str) -> None:
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
        if isinstance(key, Unnamed):
            # The pane may still read it: F13 to a pane that asked for
            # every key, or ctrl+1 to one that disambiguates. Only the
            # binding for this key takes it, so it never self-inserts.
            # Lillecarl/pymux#707.
            self._say_it_has_no_name(key, insert_text)
            super()._call_handler(Keys.UnnamedKey, insert_text)
            return
        # A cursor position report answers a probe of the server's: a
        # full screen application asks for none of its own.
        # Lillecarl/pymux#539.
        if key is TERMINAL_REPLY or key is Keys.CPRResponse:
            if self.reply_callback is not None:
                self.reply_callback(insert_text)
            return
        if key is Keys.Vt100MouseEvent and self.pixel_cell is not None:
            cell = self.pixel_cell()
            if cell is not None:
                converted = pixels_to_cells(insert_text, *cell)
                if converted is None:
                    return
                insert_text, *fraction = converted
                self.pointer_fraction = (fraction[0], fraction[1])
        if key is Keys.Vt100MouseEvent:
            report = SgrMouseReport.parse(insert_text)
            if report is not None:
                self.pointer_cell = (report.column - 1, report.row - 1)
                if self.pointer_moved is not None:
                    self.pointer_moved()
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
        if key is Keys.Escape and not self._one_key_of_several and insert_text.startswith(CSI):
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
