"""
What pymux tells a pane about the keyboards of its clients.

A pane asks the terminal what it does, and the answer has to hold. Two
flags of the kitty keyboard protocol need a terminal that speaks the
protocol: the event type of a key and the other codes of a key. pymux
makes up what a legacy keyboard cannot send, so a pane keeps both. It
still has to know what the terminal of every client can report: a
keyboard that sends its own key release may not get a second one.
"""
import pytest
from pyte.keys import KeyboardFlag

from pymux.main import Pymux
from pymux.options import ALL_OPTIONS, ExtendedKeys, SetOptionError


class FakeConnection:
    "A client connection, with the mask that its detection found."

    def __init__(self, flags=0):
        self.kitty_source_flags = flags


class FakeClientState:
    pass


class FakeScreen:
    def __init__(self):
        self.keyboard_source_flags = 0
        self.synthesize_key_events = False
        self.extended_keys_allowed = True


class FakePane:
    def __init__(self, pane_id):
        self.pane_id = pane_id
        self.screen = FakeScreen()


def make_pymux(*masks):
    "A pymux with one attached client for each mask."
    pymux = Pymux()
    connections = [FakeConnection(mask) for mask in masks]
    pymux._client_states = {
        connection: FakeClientState() for connection in connections
    }
    return pymux, connections


# ----------------------------------------------------------------------
# The mask of the clients.


def test_no_client_reports_nothing():
    assert Pymux().keyboard_source_flags() == 0


def test_one_client_reports_what_its_terminal_took():
    pymux, _ = make_pymux(0b11111)
    assert pymux.keyboard_source_flags() == 0b11111


def test_a_legacy_client_holds_the_others_back():
    "A key can come from any client, so only what all of them serve counts."
    pymux, _ = make_pymux(0b11111, 0)
    assert pymux.keyboard_source_flags() == 0


def test_two_clients_report_what_they_share():
    pymux, _ = make_pymux(0b11111, 0b00011)
    assert pymux.keyboard_source_flags() == 0b00011


def test_a_connection_that_never_attached_does_not_count():
    "It runs one command and leaves. No terminal of a user is behind it."
    pymux, _ = make_pymux(0b11111)
    pymux.connections = list(pymux._client_states) + [FakeConnection(0)]
    assert pymux.keyboard_source_flags() == 0b11111


def test_a_client_that_leaves_lets_the_rest_speak():
    pymux, connections = make_pymux(0b11111, 0)
    assert pymux.keyboard_source_flags() == 0
    pymux.remove_client(connections[1])
    assert pymux.keyboard_source_flags() == 0b11111


# ----------------------------------------------------------------------
# Telling the panes.


def test_every_pane_hears_the_mask():
    pymux, _ = make_pymux(0b11111, 0b00011)
    panes = [FakePane(1), FakePane(2)]
    for pane in panes:
        pymux.panes_by_id[pane.pane_id] = pane

    pymux.sync_keyboard_source_flags()
    for pane in panes:
        assert pane.screen.keyboard_source_flags == 0b00011


def test_a_pane_that_starts_later_hears_it_as_well():
    pymux, _ = make_pymux(0b00110)
    pane = FakePane(1)
    pymux.tell_pane_about_the_keyboard(pane)
    assert pane.screen.keyboard_source_flags == 0b00110


def test_a_client_that_leaves_raises_the_mask_of_a_pane():
    pymux, connections = make_pymux(0b11111, 0)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane
    pymux.sync_keyboard_source_flags()
    assert pane.screen.keyboard_source_flags == 0

    pymux.remove_client(connections[1])
    assert pane.screen.keyboard_source_flags == 0b11111


def test_a_pane_without_a_process_is_no_error():
    "A pane can be told before or after its process. Neither may raise."

    class PaneWithoutProcess:
        pane_id = 1

        @property
        def process(self):
            raise AttributeError("no process yet")

    pymux, _ = make_pymux(0b11111)
    pymux.tell_pane_about_the_keyboard(PaneWithoutProcess())  # Does not raise.


def test_a_screen_that_knows_nothing_about_the_host_is_no_error():
    "An older ptterm has no such attribute. It then claims what a pane asks."

    class OldScreen:
        __slots__ = ()

    class OldPane:
        pane_id = 1

        def __init__(self):
            self.screen = OldScreen()

    pymux, _ = make_pymux(0b11111)
    pymux.tell_pane_about_the_keyboard(OldPane())  # Does not raise.


# ----------------------------------------------------------------------
# The option.


def test_making_up_key_events_is_on_by_default():
    assert Pymux().synthesize_key_events is True


def test_every_pane_hears_the_option():
    pymux, _ = make_pymux(0)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane

    pymux.sync_keyboard_source_flags()
    assert pane.screen.synthesize_key_events is True

    pymux.synthesize_key_events = False
    pymux.sync_keyboard_source_flags()
    assert pane.screen.synthesize_key_events is False


def test_the_option_alone_reaches_the_panes():
    "The mask does not change with it, and the panes still hear it."
    pymux, _ = make_pymux(0b11111)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane
    pymux.sync_keyboard_source_flags()

    pymux.synthesize_key_events = False
    pymux.sync_keyboard_source_flags()
    assert pane.screen.synthesize_key_events is False
    assert pane.screen.keyboard_source_flags == 0b11111


# ----------------------------------------------------------------------
# What pymux asks the terminal of a client for.


def test_pymux_asks_the_terminal_to_disambiguate():
    """
    A bare Escape is also the first byte of every escape sequence. A
    terminal that disambiguates writes the key as "CSI 27 u" instead,
    which can start nothing. pymux asks for that on its own account,
    and not only when a pane wants it. Lillecarl/pymux#164.
    """
    assert Pymux().keyboard_flags_for_a_client() == KeyboardFlag.DISAMBIGUATE


def test_what_a_pane_asks_for_reaches_the_terminal_as_well():
    "One terminal sends the keys of both, so it takes one set of flags."

    class PaneThatAsked:
        class screen:
            kitty_keyboard_flags = KeyboardFlag.REPORT_EVENT_TYPES

    pymux = Pymux()
    pymux.get_focused_pane = lambda: PaneThatAsked()
    assert pymux.keyboard_flags_for_a_client() == (
        KeyboardFlag.DISAMBIGUATE | KeyboardFlag.REPORT_EVENT_TYPES
    )


# ----------------------------------------------------------------------
# The escape hatch.


def test_extended_keys_is_on_to_begin_with():
    assert Pymux().extended_keys is ExtendedKeys.ON


def test_off_asks_the_terminal_for_nothing():
    """
    A person attaching with a terminal that claims more than it does
    steps the whole session down, and the client writes
    "CSI = 0 ; 1 u" to put it back in the legacy encoding.
    Lillecarl/pymux#173.
    """
    pymux, _ = make_pymux(0b11111)
    ALL_OPTIONS["extended-keys"].set_value(pymux, "off")

    assert pymux.keyboard_flags_for_a_client() == 0


def test_off_tells_every_pane_as_well():
    "Asking the terminal for nothing is half of it. A pane hears too."
    pymux, _ = make_pymux(0b11111)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane

    ALL_OPTIONS["extended-keys"].set_value(pymux, "off")

    assert pane.screen.extended_keys_allowed is False


def test_turning_it_back_on_reaches_the_panes():
    "The session steps back up without restarting anything."
    pymux, _ = make_pymux(0b11111)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane
    ALL_OPTIONS["extended-keys"].set_value(pymux, "off")

    ALL_OPTIONS["extended-keys"].set_value(pymux, "on")

    assert pane.screen.extended_keys_allowed is True
    assert pymux.keyboard_flags_for_a_client() == KeyboardFlag.DISAMBIGUATE


def test_always_still_asks_the_terminal():
    "It changes what a client believes about its terminal, not the flags."
    pymux, _ = make_pymux(0b11111)
    ALL_OPTIONS["extended-keys"].set_value(pymux, "always")

    assert pymux.keyboard_flags_for_a_client() == KeyboardFlag.DISAMBIGUATE


def test_a_value_nobody_defines_is_an_error():
    pymux, _ = make_pymux(0)
    with pytest.raises(SetOptionError):
        ALL_OPTIONS["extended-keys"].set_value(pymux, "sometimes")
