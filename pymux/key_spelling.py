"""
How a person writes a key, and the keys of the press that names.

A key name has to say two things, and the spelling pymux took from tmux
says neither.

**Which keys are pressed together.** "C-a" is ctrl and a, and a reader
has to know already that "C-" is a modifier and not the start of a
name. Every keyboard configuration a person meets writes this with a
plus: kitty writes "ctrl+shift+a", niri and Sway write "Mod+Shift+H".
So this does too, and the muscle memory carries over.

**Which keys are pressed one after another.** tmux has no spelling for
this at all. `bind-key` takes one key and the prefix before it is a
flag, so a binding of two steps is written in prose; and `send-keys C-a
C-b` reads exactly as one chord would.

    ctrl+shift+a        together: one press
    prefix b w          after: three presses, in order
    ctrl+b w            a chord, and then a key
    alt+f5              the modifiers go in any order
    Home  DC  PPage     the names tmux gives a key still read

**The chord is built and not looked up.** `keys.the_key_named` is the
one translation from a base key and its modifier bits to the key
prompt_toolkit binds, and the reader of a real key press uses the same
one. A second table of every combination is what Lillecarl/pymux#234
cost ctrl+Home: `PYMUX_TO_PROMPT_TOOLKIT_KEYS` writes out `C-Left`,
`C-Right`, `C-Up` and `C-Down`, so ctrl on the four arrows worked and
ctrl on the other twenty-two named keys did not.

**Nothing here holds any pymux state.** The prefix is a step of the
grammar, and what it is bound to is the caller's to supply. So this
module reads a name, and a name only.
"""

import re
from typing import Dict, Sequence, Tuple

from prompt_toolkit.completion import Completer, Completion
from pyte.keys import KeyEvent, Modifier

from .key_mappings import (
    PYMUX_TO_PROMPT_TOOLKIT_KEYS,
    THE_MODIFIERS_A_PERSON_WRITES,
    pymux_key_to_prompt_toolkit_key_sequence,
)
from .keys import (
    A_KEY_BY_ITS_NAME,
    KEYS_A_KEYBOARD_SPELLS_OUT,
    THE_NAME_OF_A_KEY,
    Dropped,
    an_event_named,
    the_key_named,
)

__all__ = [
    "AFTER",
    "KeyCompleter",
    "MODIFIERS_A_PERSON_WRITES",
    "THE_ALIASES",
    "THE_PREFIX",
    "THE_SAME_MODIFIER",
    "TOGETHER",
    "a_chord",
    "a_key_however_it_is_written",
    "an_event",
    "an_event_however_it_is_written",
    "as_a_chord",
    "keys_of",
    "the_events_of",
]


#: What joins the keys of one press.
TOGETHER = "+"

#: What separates one press from the next.
#:
#: A space is what a person says out loud, and it is what `send-keys`
#: already separates its keys by, because shlex does. So the shape of
#: that command does not change: only the spelling of one key gains a
#: plus.
#:
#: kitty writes ">" here and several configurations write a comma. Both
#: of those are keys a person can press, and would then have to escape.
#: A space is not: the space key is written "Space".
AFTER = " "

#: The step that means the prefix key of this server, whatever it is
#: bound to.
#:
#: It is a step and never a modifier. A person presses the prefix, lets
#: it go, and then presses the next key, so "prefix b" says what
#: happens and "prefix+b" does not.
THE_PREFIX = "prefix"


#: The two modifiers that are not part of a key name.
#:
#: Caps lock on a letter is the capital, which is a character and not a
#: key of its own, and num lock is the same for the keypad. Neither can
#: be held down with a key the way ctrl can.
_THE_LOCKS = Modifier.CAPS_LOCK | Modifier.NUM_LOCK


#: What a person writes for each modifier, and the bit it means.
#:
#: The names are the members of `Modifier`, and the rest are what a
#: keyboard prints on the key: a Mac prints "option" and "command", a
#: PC prints "win". Nobody has to find out which word this program
#: chose.
#:
#: **"meta" is not alt here**, although tmux's "M-" is. The protocol
#: has both and they are different bits, so "alt" is alt and "meta" is
#: meta. `THE_MODIFIERS_A_PERSON_WRITES` in `key_mappings.py` keeps the
#: tmux reading for the tmux spelling.
MODIFIERS_A_PERSON_WRITES: Dict[str, Modifier] = {
    **{
        modifier.name.lower(): modifier
        for modifier in Modifier
        if not modifier & _THE_LOCKS
    },
    "control": Modifier.CTRL,
    "option": Modifier.ALT,
    "opt": Modifier.ALT,
    "command": Modifier.SUPER,
    "cmd": Modifier.SUPER,
    "win": Modifier.SUPER,
}


def _the_aliases() -> Dict[str, str]:
    """
    The base keys tmux gives a name of its own, and the name the
    toolkit uses.

    "DC" is delete, "PPage" is page up and "BSpace" is backspace. No
    table knows those but `PYMUX_TO_PROMPT_TOOLKIT_KEYS`, so they are
    read off it rather than written out again: an entry with no
    modifier in its name, whose value is one key, is a base key under
    another name.
    """
    aliases: Dict[str, str] = {}
    for name, sequence in PYMUX_TO_PROMPT_TOOLKIT_KEYS.items():
        if len(sequence) != 1 or "-" in name:
            continue
        base = THE_NAME_OF_A_KEY.get(sequence[0], sequence[0])
        if name.lower() != base:
            aliases[name.lower()] = base
    return aliases


THE_ALIASES = _the_aliases()


def _is_a_base_name(name: str) -> bool:
    """
    Whether a toolkit name is a key on its own, with nothing held down.

    **The chord writes the modifier, so the base may not.** "c-a" is a
    `Keys` member and reads back as a name, so without this "C-a" would
    read here and "M-x" would not: one tmux spelling working by
    accident. One grammar or the other. `key_mappings.py` reads the
    tmux spelling and this reads the chord, and a caller that wants
    both asks both.

    "<any>" and "<bracketed-paste>" are the other kind of member: a key
    that no keyboard sends and nobody writes.
    """
    return "-" not in name and "<" not in name


def _the_base_named(text: str) -> str:
    """
    The base key a written name means, without any modifiers.

    Raises `ValueError` when no key has that name.
    """
    lowered = text.lower()
    alias = THE_ALIASES.get(lowered)
    if alias is not None:
        return alias
    if lowered in A_KEY_BY_ITS_NAME and _is_a_base_name(lowered):
        return lowered
    if len(text) == 1:
        # A character key is itself, and its case is kept: "A" is a
        # capital, and "shift+a" is the same key written the other way.
        return text
    raise ValueError("No key is named %r." % (text,))


def a_chord(text: str) -> Tuple[str, ...]:
    """
    The prompt_toolkit keys of one press, such as "ctrl+shift+a".

    It is a tuple because alt is two: prompt_toolkit spells alt as an
    escape and the key, and binds it that way. Everything else is a
    tuple of one.

    Raises `ValueError` for a name that no key has, and for a
    combination prompt_toolkit cannot spell.
    """
    mods, base = _the_modifiers_and_base_of(text)
    key = the_key_named(base, mods)
    if isinstance(key, Dropped):
        raise ValueError("No name here for the key %r: %s." % (text, key.reason))
    return key if isinstance(key, tuple) else (key,)


def keys_of(text: str, prefix: Sequence[str] = ()) -> Tuple[str, ...]:
    """
    Every prompt_toolkit key that this text names, in the order they
    are pressed.

    One flat tuple, which is what `KeyBindings.add` takes and what
    `send-keys` writes out one at a time. A chord of its own already
    flattens -- alt is an escape and the key -- so "alt+a b" and
    "escape a b" give the same thing and nothing here can tell them
    apart afterwards. This reads a name; it does not write one.

    `prefix` is the keys the prefix of this server is bound to, which
    only the caller knows. A text that says "prefix" without it raises.

    Raises `ValueError` for anything it cannot read.
    """
    keys: list[str] = []

    for step in text.split(AFTER):
        if not step:
            # Two spaces, or a space at either end. A person writing a
            # sequence should not have to count them.
            continue
        if step.lower() == THE_PREFIX:
            if not prefix:
                raise ValueError(
                    "%r names the prefix, and no prefix was given." % (text,)
                )
            keys.extend(prefix)
            continue
        keys.extend(a_chord(step))

    if not keys:
        raise ValueError("%r names no key." % (text,))
    return tuple(keys)


def _the_modifiers_and_base_of(text: str) -> Tuple[int, str]:
    """
    The modifier bits and the base key that one chord names.

    Raises `ValueError` for a name that no key has, and for a word that
    is not a modifier.
    """
    parts = text.split(TOGETHER)

    if len(parts) >= 2 and parts[-1] == "":
        if parts[-2] == "":
            # The plus key itself, which is written "+", and with
            # modifiers "ctrl++". Splitting leaves two empty parts
            # where the key was.
            parts = parts[:-2] + [TOGETHER]
        else:
            raise ValueError("%r names no key after the %r." % (text, TOGETHER))

    mods = 0
    for part in parts[:-1]:
        modifier = MODIFIERS_A_PERSON_WRITES.get(part.lower())
        if modifier is None:
            raise ValueError("%r is not a modifier, in %r." % (part, text))
        mods |= modifier

    return mods, _the_base_named(parts[-1])


def an_event(text: str) -> KeyEvent:
    """
    The key event that one chord is, such as "ctrl+shift+a".

    **The form a key goes to a pane in.** The other reading, into
    prompt_toolkit keys, is for binding and never reaches a pane. The
    two used to be one road, through the legacy encoding, which lost
    everything that encoding cannot carry. Lillecarl/pymux#237.

    Raises `ValueError` for a name that no key has.
    """
    mods, base = _the_modifiers_and_base_of(text)
    return an_event_named(base, mods)


def the_events_of(text: str, prefix: Sequence[KeyEvent] = ()) -> Tuple[KeyEvent, ...]:
    """
    Every key event this text names, in the order they are pressed.

    The counterpart of `keys_of`, for the road to a pane.
    """
    events: list[KeyEvent] = []

    for step in text.split(AFTER):
        if not step:
            continue
        if step.lower() == THE_PREFIX:
            if not prefix:
                raise ValueError(
                    "%r names the prefix, and no prefix was given." % (text,)
                )
            events.extend(prefix)
            continue
        events.append(an_event(step))

    if not events:
        raise ValueError("%r names no key." % (text,))
    return tuple(events)


#: How the older spelling writes each modifier, and the word this one
#: writes.
#:
#: The prefixes are the ones `key_mappings.py` reads, so the two agree
#: by construction, and the words are the members of `Modifier`, so
#: they agree with the chord table above.
#:
#: "M-" is the one that has to be written in. tmux means alt by it, and
#: the protocol's meta is a different bit, so the letter is tmux's and
#: the word is the protocol's. That is also why "meta+" here is
#: `Modifier.META` and not alt.
THE_SAME_MODIFIER = {
    **{
        spelling: modifier.name.lower()
        for spelling, modifier in THE_MODIFIERS_A_PERSON_WRITES
    },
    "m-": "alt",
}


def as_a_chord(name: str) -> str:
    """
    A key name in the older spelling, written as a chord.

    "C-S-a" is "ctrl+shift+a", "M-C-Left" is "alt+ctrl+Left" and
    "Super-a" is "super+a". A name with no modifier prefix on it is
    already a chord of one key.
    """
    modifiers = []
    found = True
    while found:
        found = False
        for spelling, word in THE_SAME_MODIFIER.items():
            if len(name) > len(spelling) and name.lower().startswith(spelling):
                modifiers.append(word)
                name = name[len(spelling) :]
                found = True
                break
    return "".join(modifier + TOGETHER for modifier in modifiers) + name


def a_key_however_it_is_written(text: str) -> Tuple[str, ...]:
    """
    The prompt_toolkit keys of one key, in either spelling.

    Three readings, and the order matters.

    The chord first, so "ctrl+home" reads. Then the tmux table, which
    holds every name that spelling ever reached, so nothing a
    configuration file already says can change meaning. Then the tmux
    name re-spelled as a chord, which is what fixes ctrl+Home: "C-Home"
    is in no table, and until now `send-keys C-Home` typed the six
    letters into the pane. Lillecarl/pymux#234.

    The third reading only ever sees a name the second one refused, so
    it can add keys and cannot change one.

    **One key, and never a sequence.** `send-keys` sends what it cannot
    read as literal text, the way tmux does, so reading a space here
    would turn `send-keys "a b"` from three characters into two key
    presses. A sequence needs `keys_of`, and needs `bind-key` to take
    one first.

    Raises `ValueError` when no reading of it names a key.
    """
    try:
        return a_chord(text)
    except ValueError:
        pass

    try:
        return pymux_key_to_prompt_toolkit_key_sequence(text)
    except ValueError:
        return a_chord(as_a_chord(text))


def an_event_however_it_is_written(text: str) -> KeyEvent:
    """
    The key event of one key, in either spelling.

    The road to a pane; `a_key_however_it_is_written` is the road to a
    binding. The tmux table is not consulted and need not be: every
    name in it is a chord once the modifier letters are re-spelled.

    Raises `ValueError` when no reading of it names a key.
    """
    try:
        return an_event(text)
    except ValueError:
        return an_event(as_a_chord(text))


def _the_other_names_of() -> Dict[str, list]:
    "Every tmux name of a base key, under the name the toolkit uses."
    others: Dict[str, list] = {}
    for alias, base in THE_ALIASES.items():
        others.setdefault(base, []).append(alias)
    return others


_THE_OTHER_NAMES_OF = _the_other_names_of()

#: A name and the number at the end of it, so that "f2" sorts before
#: "f10". Sorting the strings puts "f1", "f10", "f11" and then "f2",
#: which is a list nobody can read.
_A_NUMBER_AT_THE_END = re.compile(r"^(.*?)(\d*)$")


def _in_reading_order(names) -> list:
    "The names sorted the way a person reads them."

    def parts(name: str):
        head, digits = _A_NUMBER_AT_THE_END.match(name).groups()
        return head, int(digits) if digits else 0

    return sorted(names, key=parts)


def _the_names_offered() -> list:
    """
    Every name a completer offers, with what to say beside it, in the
    order they are offered.

    **The order is the feature.** Lillecarl/pymux#220 exists because a
    laptop has no Home key and no function row, so those are the names
    a person comes here for and they come first. The rest are reachable
    and are not in the way.

    Not every name that parses is offered. `A_KEY_BY_ITS_NAME` holds
    every `Keys` member, and most of them are a key that already
    carries a modifier ("c-a", "s-up") or a thing that is not a key at
    all ("<any>", "<bracketed-paste>"). A chord writes the modifier
    itself, so offering the toolkit's own spelling beside it would give
    two ways to write one key and a list twice as long.
    """
    plain = [
        name for name in A_KEY_BY_ITS_NAME if _is_a_base_name(name) and len(name) > 1
    ]
    spelled_out = _in_reading_order(KEYS_A_KEYBOARD_SPELLS_OUT)
    rest = _in_reading_order(set(plain) - KEYS_A_KEYBOARD_SPELLS_OUT)

    offered = [
        (name, ", ".join(_THE_OTHER_NAMES_OF.get(name, [])))
        for name in spelled_out + rest
    ]
    # The names tmux gives the same keys, last: they still read, and a
    # person who knows them should find them, but they are a second
    # spelling of a key that is already in the list.
    offered += [
        (alias, "the same key as %r" % (base,))
        for alias, base in sorted(THE_ALIASES.items())
    ]
    return offered


_THE_NAMES_OFFERED = _the_names_offered()


class KeyCompleter(Completer):
    """
    The keys and the modifiers a person can write, offered as they
    type.

    It completes the word under the cursor, which is one segment of one
    chord: what comes after the last plus, of the step after the last
    space. So "ctrl+ho" completes to "ctrl+home" and leaves the "ctrl+"
    where it is.

    A modifier already held is not offered a second time, under any of
    its spellings: "ctrl+control" is one modifier written twice.

    `offer_the_prefix` says whether "prefix" is one of the answers. It
    is a step of the grammar, so a box that composes a binding wants
    it. A box that sends a key to a pane does not: the prefix is what
    pymux keeps for itself, and `send-prefix` is the command that sends
    it on.
    """

    def __init__(self, offer_the_prefix: bool = True) -> None:
        self.offer_the_prefix = offer_the_prefix

    def get_completions(self, document, complete_event):
        step = document.text_before_cursor.rsplit(AFTER, 1)[-1]
        written, _, word = step.rpartition(TOGETHER)
        lowered = word.lower()
        start = -len(word)

        held = 0
        for part in written.split(TOGETHER):
            held |= MODIFIERS_A_PERSON_WRITES.get(part.lower(), 0)

        def offer(name: str, meta: str):
            if name.lower().startswith(lowered):
                yield Completion(name, start_position=start, display_meta=meta)

        if self.offer_the_prefix and not written:
            # The prefix is a step of its own and never a modifier, so
            # it is offered only where a step starts.
            yield from offer(THE_PREFIX, "the prefix key of this server")

        for name, meta in _THE_NAMES_OFFERED:
            yield from offer(name, meta)

        for name, modifier in MODIFIERS_A_PERSON_WRITES.items():
            if not modifier & held:
                yield from offer(name, "held down")
