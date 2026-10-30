"""
A session: the windows somebody arranged, and the clients on them.

One server holds many of these. tmux says the same about itself -- "all
sessions are managed by a single server" -- and pymux held exactly one
until Lillecarl/pymux#323.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from prompt_toolkit.data_structures import Size
from pyte.keep import Keep

from .arrangement import Arrangement
from .ids import SessionId
from .session_options import SessionOptions
from .window_tree import Pane

if TYPE_CHECKING:
    from .tags import Tags

__all__ = ["DEFAULT_SIZE", "OwnOptions", "Session"]

#: How big a window of this session is while no client is watching it.
#:
#: A window exists before a client attaches, and a program in a pane needs
#: a size from the first byte it writes. tmux answers the same two numbers
#: and calls them `default-size` (`cmd-new-session.c`, `dsx` and `dsy`).
DEFAULT_SIZE = Size(rows=24, columns=80)


if TYPE_CHECKING:
    # A checker reads a session's options as the declaration's types.
    _Typed = SessionOptions
else:
    _Typed = object


class OwnOptions(_Typed):
    """
    One session's options: the values it set itself, over the defaults.

    A value written here is this session's own. A name it does not hold
    reads `Pymux.session_defaults`, so a later `set-option -g` reaches
    every session that never set its own: tmux's `options_get` walks to
    the parent table the same way (`options.c`). `forget` drops an own
    value, which is what `-gg` does to every session.
    Lillecarl/pymux#756.

    The names are `SessionOptions`', each read through `_Default`.
    """

    def __init__(self, defaults: SessionOptions) -> None:
        self._defaults = defaults

    def own(self) -> dict[str, object]:
        "The values this session set itself, by attribute name."
        return {name: value for name, value in vars(self).items() if name in _SESSION_OPTION_NAMES}

    def holds(self, name: str) -> bool:
        "Whether this session set `name` itself."
        return name in vars(self)

    def forget(self, name: str) -> None:
        "Drop this session's own value, so it reads the default again."
        vars(self).pop(name, None)


_SESSION_OPTION_NAMES = frozenset(each.attribute for each in SessionOptions.DECLARED.values())


class _Default:
    """
    A session option this session did not set: the default.

    Not a data descriptor, so an own value in the instance's dict wins
    with no call at all. Not `__getattr__` either: that runs after a
    lookup has failed and raised, and the status line reads dozens of
    options a frame -- 1099 instructions of a keystroke's render.
    Lillecarl/pymux#770.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __get__(self, instance: OwnOptions | None, owner: type) -> object:
        if instance is None:
            return self
        return getattr(instance._defaults, self.name)


for _name in _SESSION_OPTION_NAMES:
    setattr(OwnOptions, _name, _Default(_name))
del _name


class Session:
    """
    One session of a server.

    The windows are the session's. Which window a client looks at is the
    client's, and `Arrangement` keeps that per application.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "session_id": Keep.SAVED,
        "name": Keep.SAVED,
        "options": Keep.SAVED,
        "arrangement": Keep.SAVED,
        "environment": Keep.SAVED,
        "tags": Keep.SAVED,
        "default_size": Keep.SAVED,
        "overlay_pane": Keep.SAVED,
        "overlay_title": Keep.SAVED,
        "overlay_width": Keep.SAVED,
        "overlay_height": Keep.SAVED,
        "created": Keep.SAVED,
        "last_used": Keep.SAVED,
    }

    def __init__(self, session_id: SessionId, name: str, defaults: SessionOptions) -> None:
        #: The number in `$0`, the way tmux spells a session. It counts
        #: from zero for each server, and no two sessions of one server
        #: ever share it, including after one is killed.
        self.session_id = session_id

        self.name = name
        self.options = OwnOptions(defaults)
        self.arrangement = Arrangement(self.options)

        #: What `set-environment` without `-g` fills. A new pane of this
        #: session reads this over the global environment.
        #: Lillecarl/pymux#270.
        self.environment: dict[str, str | None] = {}

        #: What a caller said about this session, with `set-tag -s`.
        self.tags: Tags = {}

        #: The overlay pane: a pane that floats in the middle of the
        #: screen over the layout, like the popup of tmux. It belongs
        #: to the session, so every client of this session sees the
        #: same one, and it takes the keyboard while it is open.
        #: Lillecarl/pymux#324.
        self.overlay_pane: Pane | None = None
        self.overlay_title = ""
        self.overlay_width: str | None = None
        self.overlay_height: str | None = None

        #: How big a window of this session is while nobody is watching
        #: it, when `new-session -x -y` named a size. `None` when nothing
        #: did: each window then keeps the size the clients' `window-size`
        #: gave it when it was made (`Window.size_when_made`), and with
        #: no client, `DEFAULT_SIZE`. Lillecarl/pymux#817.
        #:
        #: **Not `window.manual_size`.** A manual size says a person
        #: means that size to stay, so a client that attaches keeps it
        #: and scrolls. This one gives way to the client, which is what
        #: `window-size` is for and what tmux does with the same flags.
        #: Lillecarl/pymux#459.
        self.default_size: Size | None = None

        self.created = time.time()

        #: The turn of `Pymux.client_was_used` when a person last looked
        #: at this session. A client that attaches with no session named
        #: lands on the highest.
        self.last_used = 0

    def __repr__(self) -> str:
        return "Session($%s, %r)" % (self.session_id, self.name)
