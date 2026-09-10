"""
The spelling that says which keys are pressed together, and which after.

Two things this has to get right, and they pull against each other. It
has to read everything the tmux spelling already reads, because a
person who knows "C-a" should not have to learn a second name for the
same key. And it has to read what the tmux spelling cannot: ctrl+Home
is the key Lillecarl/pymux#220 is named for, and
Lillecarl/pymux#234 found that `send-keys C-Home` types the six
letters into the pane, because the table stops at the four arrows.

So the parity test below is mechanical. Every entry of
`PYMUX_TO_PROMPT_TOOLKIT_KEYS` is re-spelled as a chord and has to
reach the same keys. Nothing is written out twice.
"""

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from pymux.key_mappings import (
    PYMUX_TO_PROMPT_TOOLKIT_KEYS,
    prompt_toolkit_key_to_vt100_key,
)
from pymux.key_spelling import (
    AFTER,
    MODIFIERS_A_PERSON_WRITES,
    THE_PREFIX,
    TOGETHER,
    KeyCompleter,
    a_chord,
    a_key_however_it_is_written,
    a_key_written_out,
    an_event,
    as_a_chord,
    keys_of,
    why_a_pane_cannot_read,
)
from pymux.keys import KEYS_A_KEYBOARD_SPELLS_OUT
from pyte.keys import FIRST_FUNCTIONAL_KEY, KeyEvent, Modifier

# ----------------------------------------------------------------------
# Everything the older spelling reaches.


@pytest.mark.parametrize("name", sorted(PYMUX_TO_PROMPT_TOOLKIT_KEYS))
def test_a_chord_reaches_what_the_tmux_name_reaches(name):
    """
    The whole vocabulary, re-spelled and compared.

    This is the test that says the chord grammar loses nothing. It is
    written as a walk over the table and not as a list of examples, so
    a key added to the table is covered the day it is added.
    """
    assert a_chord(as_a_chord(name)) == PYMUX_TO_PROMPT_TOOLKIT_KEYS[name]


@pytest.mark.parametrize(
    "written,name",
    [
        ("DC", "delete"),
        ("IC", "insert"),
        ("PPage", "pageup"),
        ("NPage", "pagedown"),
        ("PgUp", "pageup"),
        ("BSpace", "backspace"),
        ("BTab", "s-tab"),
    ],
)
def test_the_names_tmux_gives_a_key_still_read(written, name):
    "A configuration written for tmux keeps working, one key at a time."
    assert a_chord(written) == a_chord(name.replace("s-", "shift+"))


def test_a_tmux_modifier_is_not_a_chord():
    """
    "C-a" does not read here, and it must not.

    It is a `Keys` member whose value happens to be "c-a", so without a
    guard it would read while "M-x" would not: one spelling working by
    accident and the other failing. `key_mappings.py` reads the tmux
    spelling; this reads the chord; a caller that wants both asks both.
    """
    with pytest.raises(ValueError):
        a_chord("C-a")
    with pytest.raises(ValueError):
        a_chord("M-x")


# ----------------------------------------------------------------------
# What the older spelling cannot reach. Lillecarl/pymux#234.


@pytest.mark.parametrize(
    "written,key",
    [
        ("ctrl+home", Keys.ControlHome),
        ("ctrl+end", Keys.ControlEnd),
        ("ctrl+delete", Keys.ControlDelete),
        ("ctrl+insert", Keys.ControlInsert),
        ("ctrl+pageup", Keys.ControlPageUp),
        ("ctrl+f5", Keys.ControlF5),
        ("ctrl+f13", Keys.ControlF13),
        ("shift+home", Keys.ShiftHome),
        ("shift+left", Keys.ShiftLeft),
        ("ctrl+shift+end", Keys.ControlShiftEnd),
        ("shift+ctrl+end", Keys.ControlShiftEnd),
        ("ctrl+shift+delete", Keys.ControlShiftDelete),
    ],
)
def test_a_modifier_on_a_key_the_older_table_never_named(written, key):
    """
    The keys ctrl+Home is one of.

    `PYMUX_TO_PROMPT_TOOLKIT_KEYS` names `C-Left`, `C-Right`, `C-Up`
    and `C-Down` and no other modified functional key, so ctrl worked
    on four keys and on none of the other twenty-two.
    """
    assert a_chord(written) == (key,)


@pytest.mark.parametrize("written", ["ctrl+home", "ctrl+f5", "shift+left"])
def test_the_key_it_names_has_bytes_to_send(written):
    """
    A name that reads is not enough: `send-keys` needs the bytes.

    Every one of these had bytes all along. Only the name was missing,
    which is why the fault was invisible.
    """
    (key,) = a_chord(written)
    assert prompt_toolkit_key_to_vt100_key(key).startswith("\x1b[")


# ----------------------------------------------------------------------
# The modifiers.


def test_the_modifiers_go_in_any_order():
    "A person writes what they hold down, not what this file sorts."
    assert a_chord("ctrl+shift+a") == a_chord("shift+ctrl+a")


def test_alt_is_an_escape_and_the_key():
    """
    prompt_toolkit has no member for alt and no name for it: it spells
    alt as two key presses and binds it that way.
    """
    assert a_chord("alt+a") == (Keys.Escape, "a")
    assert a_chord("alt+f1") == (Keys.Escape, Keys.F1)


def test_meta_is_not_alt():
    """
    tmux writes "M-" for alt. The protocol has both, and they are
    different bits, so this spelling keeps them apart.
    """
    assert a_chord("meta+a") != a_chord("alt+a")
    assert a_chord("meta+a") == ("meta-a",)


def test_super_hyper_and_meta_get_a_built_name():
    "No `Keys` member names one of these. Lillecarl/pymux#181."
    assert a_chord("super+a") == ("super-a",)
    assert a_chord("hyper+shift+up") == ("s-hyper-up",)


@pytest.mark.parametrize(
    "written,same_as",
    [("control+a", "ctrl+a"), ("option+a", "alt+a"), ("cmd+a", "super+a")],
)
def test_the_word_a_keyboard_prints_on_the_key(written, same_as):
    'A Mac prints "option" and "command". Nobody has to translate.'
    assert a_chord(written) == a_chord(same_as)


def test_shift_on_a_letter_is_the_capital():
    assert a_chord("shift+a") == ("A",)
    assert a_chord("A") == ("A",)


def test_the_four_keys_that_carry_a_control_code():
    "Enter, Tab, Backspace and Escape, and what ctrl means on each."
    assert a_chord("ctrl+enter") == (Keys.ControlJ,)
    assert a_chord("shift+tab") == (Keys.BackTab,)
    assert a_chord("ctrl+tab") == (Keys.ControlI,)
    assert a_chord("ctrl+backspace") == (Keys.Backspace,)


def test_the_plus_key_itself():
    "The character that joins a chord is also a key a person presses."
    assert a_chord(TOGETHER) == ("+",)
    assert a_chord("shift" + TOGETHER + TOGETHER) == ("+",)


# ----------------------------------------------------------------------
# Keys pressed one after another.


def test_a_sequence_is_every_key_in_order():
    assert keys_of("ctrl+b w") == (Keys.ControlB, "w")


def test_the_prefix_is_a_step_and_the_caller_supplies_it():
    """
    What the prefix is bound to is the server's, so this module never
    knows it. That keeps the spelling readable with no pymux at all.
    """
    assert keys_of("prefix b w", prefix=(Keys.ControlB,)) == (Keys.ControlB, "b", "w")


def test_the_prefix_without_a_prefix_says_so():
    with pytest.raises(ValueError, match="no prefix"):
        keys_of("prefix b")


def test_a_chord_is_one_press_and_a_sequence_is_several():
    """
    The one thing the tmux spelling cannot say. "C-b w" is two
    arguments to `send-keys` and one key to `bind-key`, and the name
    itself says neither.
    """
    assert keys_of("ctrl+b") == (Keys.ControlB,)
    assert keys_of("ctrl+b w") == (Keys.ControlB, "w")


def test_a_modifier_on_its_own_is_not_a_key():
    'A chord ends in a key, and "ctrl" is not one.'
    with pytest.raises(ValueError, match="No key is named"):
        keys_of("ctrl b")


def test_spaces_at_either_end_are_not_a_key():
    assert keys_of("  ctrl+b   w ") == keys_of("ctrl+b w")


def test_alt_flattens_the_same_way_a_sequence_does():
    """
    Alt is an escape and the key, so a chord of one already gives two.
    Nothing downstream can tell it from two presses, and nothing tries:
    this reads a name and never writes one.
    """
    assert keys_of("alt+a") == keys_of("escape a")


# ----------------------------------------------------------------------
# What it refuses, and what it says.


@pytest.mark.parametrize(
    "written,said",
    [
        ("ctrl+", "no key after"),
        ("ctrl+nope", "No key is named"),
        ("nope+a", "is not a modifier"),
        ("ctrl+f99", "No key is named"),
        ("", "names no key"),
        ("ctrl+escape", "No name here for the key"),
    ],
)
def test_what_it_cannot_read_it_says_why(written, said):
    """
    A name that goes nowhere with no reason given is what
    Lillecarl/pymux#167 is about, one layer down.
    """
    with pytest.raises(ValueError, match=said):
        keys_of(written)


# ----------------------------------------------------------------------
# The completer.


def offered(text):
    "The names the completer offers for what has been typed so far."
    return list(KeyCompleter().get_completions(Document(text), None))


def test_the_keys_a_keyboard_can_leave_out_come_first():
    """
    **The order is the feature.** A laptop has no Home key and no
    function row, so those are the names a person opens this for.
    Lillecarl/pymux#220.
    """
    names = [completion.text for completion in offered("")]
    assert names[0] == THE_PREFIX
    head = names[1 : 1 + len(KEYS_A_KEYBOARD_SPELLS_OUT)]
    assert set(head) == KEYS_A_KEYBOARD_SPELLS_OUT


def test_the_function_row_reads_in_order():
    "f2 before f10. Sorting the strings puts f1, f10, f11, then f2."
    names = [completion.text for completion in offered("f")]
    assert names[:4] == ["f1", "f2", "f3", "f4"]


def test_it_completes_the_segment_and_leaves_the_chord_alone():
    'Typing "ctrl+ho" completes to "ctrl+home", not to "home".'
    (completion,) = offered("ctrl+ho")
    assert completion.text == "home"
    assert completion.start_position == -2


def test_it_completes_the_step_and_leaves_the_ones_before_it():
    (completion,) = offered("prefix b ho")
    assert completion.text == "home"
    assert completion.start_position == -2


def test_a_modifier_already_held_is_not_offered_again():
    assert "ctrl" not in [completion.text for completion in offered("ctrl+c")]
    assert "ctrl" in [completion.text for completion in offered("shift+c")]


def test_a_modifier_is_not_offered_again_under_another_name():
    '"ctrl+control" is one modifier written twice.'
    assert "control" not in [completion.text for completion in offered("ctrl+c")]


def test_the_prefix_is_offered_only_where_a_step_starts():
    assert THE_PREFIX in [completion.text for completion in offered("pre")]
    assert THE_PREFIX not in [completion.text for completion in offered("ctrl+pre")]


def test_everything_it_offers_reads():
    """
    The invariant that keeps the two halves together: a name the
    completer puts in front of a person has to be one this module can
    read back.
    """
    for completion in offered(""):
        if completion.text == THE_PREFIX:
            continue
        if completion.text in MODIFIERS_A_PERSON_WRITES:
            assert a_chord(completion.text + TOGETHER + "a")
        else:
            assert a_chord(completion.text)


def test_it_offers_no_name_that_carries_its_own_modifier():
    """
    "c-a" is a `Keys` member and reads back as a name. Offering it
    beside "ctrl" and "a" would give two ways to write one key, and a
    list twice as long for no gain.
    """
    for completion in offered(""):
        assert "-" not in completion.text or completion.text == "s-tab"


# ----------------------------------------------------------------------
# Saying why a key did not fit. Lillecarl/pymux#238.


@pytest.mark.parametrize(
    "name",
    ["ctrl+shift+a", "super+a", "alt+f5", "ctrl+home", "shift+delete", "a", "Space"],
)
def test_a_key_is_written_the_way_it_was_read(name):
    "The name in a message is one a person could write back."
    assert an_event(a_key_written_out(an_event(name))) == an_event(name)


def test_the_modifiers_read_in_the_order_a_person_writes_them():
    "Not the order of the bits, where shift comes before ctrl."
    assert a_key_written_out(an_event("shift+ctrl+a")) == "ctrl+shift+a"


def test_a_key_that_writes_no_character_is_named_by_its_number():
    "`chr` of one is a character no keyboard has, so it says nothing."
    event = KeyEvent(FIRST_FUNCTIONAL_KEY + 20, 0, "u")

    assert "57364" in a_key_written_out(event)


def test_the_reason_names_the_key_the_pane_reads_instead():
    said = why_a_pane_cannot_read(an_event("super+a"), Modifier.SUPER, "a")

    assert said.startswith("super+a reaches this pane as a:")
    assert "no super on that key" in said


def test_the_reason_for_a_key_with_no_form_says_so():
    event = KeyEvent(FIRST_FUNCTIONAL_KEY + 20, 0, "u")

    assert "cannot reach this pane at all" in why_a_pane_cannot_read(event, 0, "")


# ----------------------------------------------------------------------
# Reading a key in whichever spelling it came in. Lillecarl/pymux#234.


@pytest.mark.parametrize("name", sorted(PYMUX_TO_PROMPT_TOOLKIT_KEYS))
def test_the_tmux_vocabulary_reads_unchanged(name):
    """
    The reading that matters most: nothing a configuration file already
    says may change meaning.
    """
    assert a_key_however_it_is_written(name) == PYMUX_TO_PROMPT_TOOLKIT_KEYS[name]


@pytest.mark.parametrize(
    "name,key",
    [
        ("C-Home", Keys.ControlHome),
        ("C-End", Keys.ControlEnd),
        ("C-DC", Keys.ControlDelete),
        ("C-PPage", Keys.ControlPageUp),
        ("C-F5", Keys.ControlF5),
        ("S-Home", Keys.ShiftHome),
        ("C-S-End", Keys.ControlShiftEnd),
    ],
)
def test_a_tmux_name_that_no_table_holds(name, key):
    """
    The bug Lillecarl/pymux#234 names. `send-keys C-Home` typed the six
    letters into the pane, because the tmux table stops at the four
    arrows and `send-keys` sends what it cannot read as literal text.

    The tmux name is re-spelled as a chord, which only happens for a
    name the table refused. So it adds keys and changes none.
    """
    assert a_key_however_it_is_written(name) == (key,)


def test_the_chord_spelling_reads_here_too():
    assert a_key_however_it_is_written("ctrl+home") == (Keys.ControlHome,)


def test_what_is_not_a_key_is_still_not_a_key():
    """
    `send-keys` sends what it cannot read as literal text. A word that
    reads as a key by accident would be typed as one.
    """
    for text in ["Hello", "M-Hello", "a b", ""]:
        with pytest.raises(ValueError):
            a_key_however_it_is_written(text)


def test_it_offers_across_the_whole_grammar():
    "One box takes a modifier, a key, the prefix and a step after it."
    text = "prefix" + AFTER + "ctrl" + TOGETHER + "home"
    assert keys_of(text, prefix=(Keys.ControlB,)) == (
        Keys.ControlB,
        Keys.ControlHome,
    )
