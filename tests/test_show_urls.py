"""
URL mode, through a client: labels over the links a pane shows, live
and in copy mode, and the letters that open or copy one.
Lillecarl/pymux#608.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import once
from test_a_session_on_a_pyte_screen import COPY_POSITION, attached, pymux, rows_of, said, shows  # noqa: F401

from pymux.url_mode import labels

LINKS = "\r\nhttps://a.io/first\r\nhttps://b.io/second\r\n"


def test_the_labels_are_foots():
    "One letter while one is enough, then two, and none starts another."
    assert labels(0) == []
    # One link: an empty label drew nothing and took every key.
    assert labels(1) == ["s"]
    assert labels(3) == ["s", "a", "d"]
    many = labels(20)
    assert many[:13] == list("adfjklewcmpgh")
    assert many[13:16] == ["ss", "sa", "sd"]
    assert not any(one != other and other.startswith(one) for one in many for other in many)


async def labelled(pymux, session, copying=False):
    with set_app(pymux.connections[-1].client_state.app):
        pane = pymux.arrangement.get_active_pane()
    pane.terminal.terminal_control.feed_output(LINKS)
    await shows(session, "b.io/second")
    if copying:
        said(session, type="input", keys="C-b [")
        await shows(session, COPY_POSITION)
    said(session, type="input", keys="C-b o")
    # The link nearest the bottom gets the first label.
    await shows(session, "sttps://b.io/second")
    await shows(session, "attps://a.io/first")


async def test_a_lower_case_label_opens_its_link(pymux, monkeypatch):
    opened = []
    monkeypatch.setattr(pymux.outward, "open_url", lambda url, confirmed=False: opened.append((url, confirmed)))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await labelled(pymux, session)

        said(session, type="input", keys="a")
        await once(lambda: opened, 10.0, "nothing was opened")
        assert opened == [("https://a.io/first", True)]
        await shows(session, "https://b.io/second")


async def test_the_labels_show_over_a_pane_beside_another(pymux):
    """
    The panes draw in a chain of deferred draws, and a float of the
    whole layout drew before them: the panes painted over every label.
    One pane alone hid it. Carl's configuration, on his split windows.
    """
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("set-option pane-border-status on")
            pymux.handle_command("set-option paint-screen on")
            pymux.handle_command("split-window -h 'sleep 1000'")
        await shows(session, "1:sleep*")
        await labelled(pymux, session)


async def test_an_upper_case_label_copies_in_copy_mode(pymux, monkeypatch):
    copied = []
    monkeypatch.setattr(pymux.outward, "write_user_clipboard", copied.append)
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await labelled(pymux, session, copying=True)

        # Neither copy mode nor the pane hears a key the labels hold:
        # "q" would leave copy mode, and Escape would too.
        said(session, type="input", keys="q")
        said(session, type="input", keys="S")
        await once(lambda: copied, 10.0, "nothing was copied")
        assert copied == ["https://b.io/second"]
        assert COPY_POSITION.search("\n".join(rows_of(session)))


async def test_escape_leaves_the_labels_and_nothing_else(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        await labelled(pymux, session, copying=True)

        said(session, type="input", keys="Escape")
        await shows(session, "https://b.io/second")
        assert COPY_POSITION.search("\n".join(rows_of(session)))
