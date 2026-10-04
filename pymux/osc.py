"""
The OSC sequences that a pane sends to the terminal of the user.

Three of them ask for something that pymux cannot give: the clipboard
(52), a desktop notification (99) and the shape of the pointer (22).
ptterm hands them over, and pymux writes them to the outer terminal of
every client. iTerm2's namespace (1337) names services one by one:
pymux serves the ones it parsed -- a browser, the clipboard, a mark --
and drops the rest, because an unchecked payload drives the terminal
of the user instead of only naming a service. The working directory
(7) and the shell integration marks (133) travel on; every terminal
that knows them reads them, and the rest ignore them.

That makes the payload of a pane reach the terminal of the user, so it
is checked first. A program in a pane writes what it wants, and a
payload that carries an escape byte can drive the terminal of the user
instead of only naming a clipboard or a notification.
"""

from __future__ import annotations

import base64
import string
from urllib.parse import unquote

__all__ = [
    "MAX_OSC_LENGTH",
    "OPEN_URL_PREFIX",
    "build_osc",
    "copy_data_of",
    "current_dir_of",
    "extension_key_of",
    "file_url_dir_of",
    "ftcs_of",
    "open_url_of",
    "request_attention_of",
    "set_user_var_of",
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
        url = base64.b64decode(param[len(OPEN_URL_PREFIX) :], validate=True).decode("utf-8")
    except ValueError, UnicodeDecodeError:
        return None

    for char in url:
        point = ord(char)
        if point < 0x20 or point == 0x7F or 0x80 <= point <= 0x9F:
            # A control character in a URL is not a URL, and it has no
            # business in a command line either.
            return None
    return url or None


def extension_key_of(param: str) -> tuple[str, str] | None:
    """
    The subcommand of "OSC 1337" and the rest of its payload, or None.

    `SetMark` comes bare, `CurrentDir=/x` carries one value and
    `SetUserVar=name=b64` two. The key is ascii letters; anything else
    is not a subcommand a pane can name.
    """
    key, eq, rest = param.partition("=")
    if not key.isascii() or not key.isalpha():
        return None
    return key, rest if eq else ""


#: The characters a user variable name may hold. The value travels
#: base64, but the name travels plain, so it stays clear of the
#: separators of the sequence.
_USER_VAR_NAME = frozenset(string.ascii_letters + string.digits + "_-.")


def set_user_var_of(rest: str) -> tuple[str, str] | None:
    """
    The name and the decoded value of `SetUserVar`, or None.

    The payload after the subcommand is `name=b64`. A name with a
    separator in it would change what the sequence means downstream,
    and a value that is not base64 decodes to nothing.
    """
    name, eq, data = rest.partition("=")
    if not eq or not name or any(char not in _USER_VAR_NAME for char in name):
        return None
    if not data or any(char not in _BASE64 for char in data):
        return None
    try:
        value = base64.b64decode(data.encode("ascii")).decode("utf-8", "replace")
    except ValueError:
        return None
    return name, value


def current_dir_of(rest: str) -> str | None:
    """
    The path of `CurrentDir`, or None for anything else.

    The path travels plain, so a control character in it is not a
    path. An empty path says nothing about where the program is.
    """
    if not rest or not _is_plain_text(rest):
        return None
    return rest


def file_url_dir_of(param: str) -> tuple[str | None, str] | None:
    """
    The host and the path of `OSC 7 ; file://host/path`, or None.

    iTerm2 reads this as RemoteHost plus CurrentDir, and kitty,
    WezTerm and VS Code read the same synonym. An empty host names
    the machine here and comes back as None. The path is
    percent-encoded on the wire, and decoding runs before the check:
    an encoded control character is still one.
    """
    if not param.startswith("file://"):
        return None
    host, sep, path = param[len("file://") :].partition("/")
    if not sep:
        return None
    path = "/" + unquote(path)
    if not _is_plain_text(path):
        return None
    return (host or None), path


#: The answers `RequestAttention` takes: bounce the dock icon until
#: told otherwise, bounce it once, withdraw the ask, or celebrate.
_REQUEST_ATTENTION = frozenset(["yes", "once", "no", "fireworks"])


def request_attention_of(param: str) -> str | None:
    """
    The answer `RequestAttention` asks for, or None.

    `yes` bounces the dock icon indefinitely, `once` bounces it a
    single time, `no` cancels a previous request and `fireworks`
    explodes fireworks at the cursor. Anything else asks nothing.
    """
    keyed = extension_key_of(param)
    if keyed is None:
        return None
    key, value = keyed
    if key != "RequestAttention" or value not in _REQUEST_ATTENTION:
        return None
    return value


def copy_data_of(param: str) -> str | None:
    """
    The base64 of `OSC 1337 ; Copy=:b64`, or None.

    The one-shot clipboard write of iTerm2, beside OSC 52. Only
    base64 goes to the clipboard of the user, the way OSC 52 reads
    it; the streaming form (`CopyToClipboard` until `EndCopy`) stays
    forward-only.
    """
    if not param.startswith("Copy=:"):
        return None
    data = param[len("Copy=:") :]
    if not data or any(char not in _BASE64 for char in data):
        return None
    return data


#: The marks `OSC 133` names: the prompt, the command typed at it, the
#: output of the command, and its end. Only the end carries a value.
_FTCS_MARKS = ("A", "B", "C", "D")


def ftcs_of(param: str) -> tuple[str, int | None] | None:
    """
    The mark of `OSC 133` and the exit status it carries, or None.

    `A` opens a prompt, `B` the command typed at it, `C` the output
    of the command and `D` its end. Only `D` carries `;status`, a
    number from 0 to 255; a bare `D` aborts the command, which clears
    the zone instead of recording a status.
    """
    letter, sep, status = param.partition(";")
    if letter not in _FTCS_MARKS:
        return None
    if letter != "D":
        return (letter, None) if not sep else None
    if not sep:
        return ("D", None)
    if not status.isdigit() or not 0 <= int(status) <= 255:
        return None
    return ("D", int(status))
