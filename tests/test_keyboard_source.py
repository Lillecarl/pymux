"""
What pymux tells a pane about the keyboards of its clients.

A pane asks the terminal what it does, and the answer has to hold. Two
flags of the kitty keyboard protocol need a terminal that speaks the
protocol: the event type of a key and the other codes of a key. So
pymux has to know what the terminal of every client can report, and a
pane may only hear what all of them serve.
"""
from pymux.main import Pymux


class FakeConnection:
    "A client connection, with the mask that its detection found."

    def __init__(self, flags=0):
        self.kitty_source_flags = flags


class FakeClientState:
    pass


class FakeScreen:
    def __init__(self):
        self.keyboard_source_flags = 0


class FakeProcess:
    def __init__(self):
        self.screen = FakeScreen()


class FakePane:
    def __init__(self, pane_id):
        self.pane_id = pane_id
        self.process = FakeProcess()


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
        assert pane.process.screen.keyboard_source_flags == 0b00011


def test_a_pane_that_starts_later_hears_it_as_well():
    pymux, _ = make_pymux(0b00110)
    pane = FakePane(1)
    pymux.tell_pane_about_the_keyboard(pane)
    assert pane.process.screen.keyboard_source_flags == 0b00110


def test_a_client_that_leaves_raises_the_mask_of_a_pane():
    pymux, connections = make_pymux(0b11111, 0)
    pane = FakePane(1)
    pymux.panes_by_id[pane.pane_id] = pane
    pymux.sync_keyboard_source_flags()
    assert pane.process.screen.keyboard_source_flags == 0

    pymux.remove_client(connections[1])
    assert pane.process.screen.keyboard_source_flags == 0b11111


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
            self.process = type("P", (), {"screen": OldScreen()})()

    pymux, _ = make_pymux(0b11111)
    pymux.tell_pane_about_the_keyboard(OldPane())  # Does not raise.
