"""
Tests for routing the answers to desktop notifications.

A program asks for a notification and names it. The terminal of the
user answers by that name, but the answer arrives at the client, which
serves every pane. pymux therefore renames a notification on the way
out and puts the name of the program back on the way in.
"""
from pymux.notifications import (
    NotificationRoutes,
    read_identifier,
    replace_identifier,
    split_payload,
)


# ----------------------------------------------------------------------
# Reading the payload.


def test_the_text_may_hold_a_semicolon():
    assert split_payload("i=1:d=0;a; b") == ("i=1:d=0", ";", "a; b")


def test_a_payload_without_a_text():
    assert split_payload("i=1:p=close") == ("i=1:p=close", "", "")


def test_the_identifier_is_read_from_the_metadata():
    assert read_identifier("i=mine") == "mine"
    assert read_identifier("d=0:i=mine:p=title") == "mine"
    assert read_identifier("i=a-b_c+d.e") == "a-b_c+d.e"


def test_a_metadata_without_an_identifier():
    assert read_identifier("d=0:p=title") is None
    assert read_identifier("") is None


def test_an_identifier_that_is_not_one_is_refused():
    "Only the characters that kitty allows."
    assert read_identifier("i=with space") is None
    assert read_identifier("i=" + "x" * 65) is None
    assert read_identifier("i=") is None


def test_the_identifier_is_replaced_in_place():
    assert replace_identifier("d=0:i=mine:p=title", "7") == "d=0:i=7:p=title"
    assert replace_identifier("i=mine", "7") == "i=7"


def test_a_metadata_without_an_identifier_is_not_changed():
    assert replace_identifier("d=0:p=title", "7") == "d=0:p=title"


# ----------------------------------------------------------------------
# Out and back.


def test_a_notification_is_renamed_on_the_way_out():
    routes = NotificationRoutes()
    assert routes.outgoing(1, "i=mine:a=report;Build ready") == (
        "i=1:a=report;Build ready"
    )


def test_the_answer_carries_the_name_of_the_program_back():
    routes = NotificationRoutes()
    routes.outgoing(4, "i=mine:a=report;Build ready")
    assert routes.incoming("i=1") == (4, "i=mine")
    assert routes.incoming("i=1:p=close;untracked") == (4, "i=mine:p=close;untracked")


def test_the_same_notification_keeps_its_name():
    "A program sends a notification in pieces, and updates it later."
    routes = NotificationRoutes()
    first = routes.outgoing(1, "i=mine:d=0;half ")
    second = routes.outgoing(1, "i=mine:d=1;a message")
    assert first.startswith("i=1:") and second.startswith("i=1:")


def test_two_panes_that_pick_the_same_name_stay_apart():
    "Panes name their notifications without knowing about each other."
    routes = NotificationRoutes()
    routes.outgoing(1, "i=build;done")
    routes.outgoing(2, "i=build;done")
    assert routes.incoming("i=1") == (1, "i=build")
    assert routes.incoming("i=2") == (2, "i=build")


def test_a_notification_without_a_name_is_not_touched():
    "The answer to one names nothing, so there is nothing to route."
    routes = NotificationRoutes()
    assert routes.outgoing(1, "d=0;a message") == "d=0;a message"
    assert routes.incoming("i=0") is None


def test_an_answer_that_names_nothing_of_ours_is_dropped():
    routes = NotificationRoutes()
    routes.outgoing(1, "i=mine;done")
    assert routes.incoming("i=999") is None
    assert routes.incoming("p=close") is None


def test_the_alive_poll_is_routed_like_the_rest():
    "kitty puts an identifier on that one for multiplexers."
    routes = NotificationRoutes()
    routes.outgoing(3, "i=poll:p=alive;")
    assert routes.incoming("i=1:p=alive;a,b,c") == (3, "i=poll:p=alive;a,b,c")


# ----------------------------------------------------------------------
# The table does not grow without end.


def test_the_oldest_notification_is_forgotten():
    routes = NotificationRoutes(limit=3)
    for number in range(5):
        routes.outgoing(1, "i=n%i;x" % number)
    assert routes.incoming("i=1") is None  # The first two are gone.
    assert routes.incoming("i=2") is None
    assert routes.incoming("i=3") == (1, "i=n2")
    assert routes.incoming("i=5") == (1, "i=n4")


def test_a_notification_that_is_sent_again_stays():
    "Sending it again makes it the newest, so it is not the first to go."
    routes = NotificationRoutes(limit=2)
    routes.outgoing(1, "i=old;x")
    routes.outgoing(1, "i=other;x")
    routes.outgoing(1, "i=old;x")  # Again: now the newest.
    routes.outgoing(1, "i=third;x")
    assert routes.incoming("i=1") == (1, "i=old")
    assert routes.incoming("i=2") is None  # "other" went instead.
