"""
How a session view's screen reads the notifications the server writes
for its terminal. Lillecarl/pyterm#1073.
"""

from __future__ import annotations

from pymux.notify.notifications import QUERY, can_close, osc99_of, split_payload
from pymux.web.notices import NOTIFICATION, NOTIFICATION_CLOSE, Notices, click_report


def test_the_screen_answers_the_query_as_kitty_does():
    "The server sends notifications to a terminal that answers, and closes them on one that can close."
    taken = Notices().take(QUERY)
    assert taken.message is None and taken.answer is not None
    assert taken.answer.startswith("\x1b]99;i=pymux:p=?;")
    _metadata, _semicolon, said = split_payload(taken.answer.removeprefix("\x1b]99;").removesuffix("\x1b\\"))
    assert can_close(said)


def test_a_notification_of_two_chunks_reaches_the_page_whole():
    "The server's own spelling: a base64 title, then the body. The page sees one."
    notices = Notices()
    title, body = osc99_of("7", "Build done", "in 3 s; all green", 1)
    assert notices.take(title).message is None
    assert notices.take(body).message == {
        "type": NOTIFICATION,
        "id": "7",
        "title": "Build done",
        "body": "in 3 s; all green",
        "urgency": 1,
    }


def test_a_close_takes_it_down():
    assert Notices().take("i=7:p=close;").message == {"type": NOTIFICATION_CLOSE, "id": "7"}


def test_a_click_is_the_report_a_terminal_sends():
    assert click_report("7") == "\x1b]99;i=7;\x1b\\"
    # Not an identifier: nothing the page sends reaches the server as an escape.
    assert click_report("7;\x1b]52;c;x") is None
    assert click_report(7) is None
