"""
The OSC sequences that a pane sends to the terminal of the user.

Three of them ask for something that pymux cannot give: the clipboard
(52), a desktop notification (99) and the shape of the pointer (22).
ptterm hands them over, and pymux writes them to the outer terminal of
every client. The fourth, iTerm2's namespace (1337), asks for the
browser of the user; pymux reads it and never writes it on.

That makes the payload of a pane reach the terminal of the user, so it
is checked first. A program in a pane writes what it wants, and a
payload that carries an escape byte can drive the terminal of the user
instead of only naming a clipboard or a notification.
"""

import base64
import string

__all__ = [
    "MAX_OSC_LENGTH",
    "OPEN_URL_PREFIX",
    "build_osc",
    "open_url_of",
]

#: The longest payload that pymux passes on. A clipboard holds a
#: document, so the limit is generous; a pane that sends more loses the
#: whole sequence. Truncating is not an option: half a base64 payload
#: writes a broken clipboard.
MAX_OSC_LENGTH = 512 * 1024

#: The selections that OSC 52 names: clipboard, primary, secondary,
#: select, and the eight cut buffers.
_CLIPBOARD_SELECTIONS = frozenset("cpqs01234567")

_BASE64 = frozenset(string.ascii_letters + string.digits + "+/=")


def build_osc(code: str, param: str) -> str | None:
    """
    The escape sequence to write to the terminal of the user, or None
    when the payload of the pane must not reach it.
    """
    if len(param) > MAX_OSC_LENGTH:
        return None
    if not _is_plain_text(param):
        return None
    if code == "52" and not _is_clipboard_payload(param):
        return None
    return "\x1b]%s;%s\x1b\\" % (code, param)


def _is_plain_text(param: str) -> bool:
    """
    True when the payload carries no control character.

    An escape byte inside the payload ends the sequence early on the
    terminal of the user, and what follows it runs as a command of its
    own. The C1 range does the same on a terminal that reads eight bit
    controls.
    """
    for char in param:
        point = ord(char)
        if point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F:
            return False
    return True


def _is_clipboard_payload(param: str) -> bool:
    """
    True for the payload of OSC 52: a selection name, a semicolon and
    base64 data. An empty payload clears the selection.

    ptterm already drops the query form. This is the second check: only
    base64 goes to the clipboard of the user.
    """
    selection, semicolon, data = param.partition(";")
    if not semicolon:
        return False
    if any(char not in _CLIPBOARD_SELECTIONS for char in selection):
        return False
    return all(char in _BASE64 for char in data)


#: How iTerm2 spells the subcommand of OSC 1337 that opens a browser.
#: The URL travels base64 after a colon.
OPEN_URL_PREFIX = "OpenURL=:"


def open_url_of(param: str) -> str | None:
    """
    The URL that an "OSC 1337 ; OpenURL=:" names, or None.

    iTerm2 asks with this sequence, and a program that wants to open a
    browser on the machine of the user may send it without knowing
    which terminal reads it: a terminal that does not know it ignores
    it. Anything that does not parse as a URL is not one.
    """
    if not param.startswith(OPEN_URL_PREFIX):
        return None

    try:
        url = base64.b64decode(param[len(OPEN_URL_PREFIX) :], validate=True).decode(
            "utf-8"
        )
    except (ValueError, UnicodeDecodeError):
        return None

    for char in url:
        point = ord(char)
        if point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F:
            # A control character in a URL is not a URL, and it has no
            # business in a command line either.
            return None
    return url or None
