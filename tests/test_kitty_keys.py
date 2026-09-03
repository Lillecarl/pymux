"""
Tests for the kitty keyboard protocol decoding in pymux.kitty.

Each test feeds a sequence (or a mix of sequences and plain text) into
the parser and checks the key presses that reach the feed_key callback.
That is the same path that the server uses for client input.
"""
from prompt_toolkit.keys import Keys

from pymux.kitty import KittyVt100Parser


def parse(data: str):
    "Feed data, return the list of (key, data) tuples."
    pressed = []
    parser = KittyVt100Parser(lambda key_press: pressed.append(key_press))
    parser.feed_and_flush(data)
    return [(kp.key, kp.data) for kp in pressed]


def test_ctrl_a():
    assert parse("\x1b[97;5u") == [(Keys.ControlA, "\x1b[97;5u")]


def test_ctrl_enter():
    # ctrl+enter has no legacy encoding: real terminals send this
    # sequence even without the protocol enabled.
    assert parse("\x1b[13;5u") == [(Keys.ControlJ, "\x1b[13;5u")]


def test_plain_enter_tab_backspace_escape():
    # Unmodified, these keys keep their legacy encoding, but a terminal
    # with "report all keys" sends them as CSI u.
    assert parse("\x1b[13u") == [(Keys.Enter, "\x1b[13u")]
    assert parse("\x1b[9u") == [(Keys.Tab, "\x1b[9u")]
    assert parse("\x1b[127u") == [(Keys.Backspace, "\x1b[127u")]
    assert parse("\x1b[27u") == [(Keys.Escape, "\x1b[27u")]


def test_plain_text_key():
    assert parse("\x1b[97u") == [("a", "\x1b[97u")]


def test_shift_text_key():
    # shift+a without reported text.
    assert parse("\x1b[97;2u") == [("A", "\x1b[97;2u")]
    # With reported text (report associated text).
    assert parse("\x1b[97;2;65u") == [("A", "\x1b[97;2;65u")]
    # shift+1 reports '!' as text.
    assert parse("\x1b[49;2;33u") == [("!", "\x1b[49;2;33u")]


def test_alt_text_key():
    # The prompt_toolkit parser represents alt+key as a tuple.
    assert parse("\x1b[97;3u") == [
        (Keys.Escape, "\x1b[97;3u"),
        ("a", ""),
    ]


def test_ctrl_shift_text_key():
    # The legacy encoding has no shift for ctrl+letter combinations.
    assert parse("\x1b[120;6u") == [(Keys.ControlX, "\x1b[120;6u")]


def test_alternate_key_codes_are_ignored():
    # "CSI 97:65 ; 2 u" carries 'a' with alternate 'A'.
    assert parse("\x1b[97:65;2u") == [("A", "\x1b[97:65;2u")]


def test_release_event_is_dropped():
    assert parse("\x1b[97;1:3u") == []
    # Press events (type 1) and repeats (type 2) come through.
    assert parse("\x1b[97;1:1u") == [("a", "\x1b[97;1:1u")]
    assert parse("\x1b[97;1:2u") == [("a", "\x1b[97;1:2u")]


def test_arrow_keys():
    # Unmodified arrows keep their legacy encoding and are handled by
    # the prompt_toolkit table.
    assert parse("\x1b[1;5D") == [(Keys.ControlLeft, "\x1b[1;5D")]
    assert parse("\x1b[1;3C") == [
        (Keys.Escape, "\x1b[1;3C"),
        (Keys.Right, ""),
    ]


def test_tilde_keys():
    assert parse("\x1b[3~") == [(Keys.Delete, "\x1b[3~")]
    assert parse("\x1b[15;5~") == [(Keys.ControlF5, "\x1b[15;5~")]


def test_keypad_keys():
    assert parse("\x1b[57413u") == [("+", "\x1b[57413u")]
    assert parse("\x1b[57414u") == [(Keys.Enter, "\x1b[57414u")]
    assert parse("\x1b[57417u") == [(Keys.Left, "\x1b[57417u")]
    # Modified keypad keys are dropped.
    assert parse("\x1b[57413;5u") == []


def test_lock_keys_are_dropped():
    assert parse("\x1b[57358u") == []


def test_plain_text_is_untouched():
    assert parse("hello") == [
        ("h", "h"),
        ("e", "e"),
        ("l", "l"),
        ("l", "l"),
        ("o", "o"),
    ]


def test_legacy_sequences_are_untouched():
    assert parse("\x1b[2~") == [(Keys.Insert, "\x1b[2~")]
    assert parse("\x01abc") == [
        (Keys.ControlA, "\x01"),
        ("a", "a"),
        ("b", "b"),
        ("c", "c"),
    ]


def test_sequence_split_over_chunks():
    "A sequence that arrives in two feeds must still parse."
    pressed = []
    parser = KittyVt100Parser(lambda key_press: pressed.append(key_press))
    parser.feed("\x1b[97;")
    parser.feed("5u")
    parser.flush()
    assert [(kp.key, kp.data) for kp in pressed] == [
        (Keys.ControlA, "\x1b[97;5u")
    ]


def test_escape_key_alone_still_works():
    "flush must still turn a lone escape into the Escape key."
    assert parse("\x1b") == [(Keys.Escape, "\x1b")]


def test_flags_reply_goes_to_reply_callback():
    "The reply of a 'CSI ? u' query is not a key press."
    pressed = []
    replies = []
    parser = KittyVt100Parser(
        lambda key_press: pressed.append(key_press),
        reply_callback=replies.append,
    )
    parser.feed_and_flush("\x1b[?5u")
    assert pressed == []
    assert replies == ["\x1b[?5u"]


def test_da1_reply_goes_to_reply_callback():
    pressed = []
    replies = []
    parser = KittyVt100Parser(
        lambda key_press: pressed.append(key_press),
        reply_callback=replies.append,
    )
    parser.feed_and_flush("\x1b[?62;1;6c")
    assert pressed == []
    assert replies == ["\x1b[?62;1;6c"]


def test_replies_are_consumed_without_callback():
    "No reply callback: replies must not garble into key presses."
    assert parse("\x1b[?5u") == []
    assert parse("\x1b[?62;1;6c") == []


def test_key_events_do_not_reach_reply_callback():
    pressed = []
    replies = []
    parser = KittyVt100Parser(
        lambda key_press: pressed.append(key_press),
        reply_callback=replies.append,
    )
    parser.feed_and_flush("\x1b[97;5u")
    assert [kp.key for kp in pressed] == [Keys.ControlA]
    assert replies == []


def test_reply_split_over_chunks():
    replies = []
    parser = KittyVt100Parser(lambda kp: None, reply_callback=replies.append)
    parser.feed("\x1b[?62;")
    parser.feed("1;6c")
    parser.flush()
    assert replies == ["\x1b[?62;1;6c"]
