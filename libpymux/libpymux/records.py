"""
The fields of a session, a window and a pane, as one read gave them.

These hold no server and do no I/O. `objects.py` and `async_objects.py`
each add a server and what an object does, on top of the same fields.
"""

from __future__ import annotations

from .operations import PANE_FIELDS, SESSION_FIELDS, WINDOW_FIELDS, Row, as_int

__all__ = ["PaneFields", "Record", "SessionFields", "WindowFields"]


def _as_bool(value: str) -> bool:
    return value == "1"


class Record:
    "The fields one read gave, as text."

    _fields: tuple[str, ...] = ()

    def __init__(self, values: Row) -> None:
        self._values = dict(values)

    def __getitem__(self, name: str) -> str:
        "One field, as the server wrote it."
        return self._values[name]

    def get(self, name: str, default: str = "") -> str:
        "One field, or `default` when the server does not know it."
        return self._values.get(name, default)

    @property
    def fields(self) -> Row:
        "Every field that this object read, as text."
        return dict(self._values)


class PaneFields(Record):
    _fields = PANE_FIELDS

    def __repr__(self) -> str:
        return "%s(%r, command=%r)" % (type(self).__name__, self.id, self.current_command)

    @property
    def id(self) -> str:
        'The pane id, as a target: "%1001".'
        return self._values["pane_id"]

    @property
    def index(self) -> int:
        "The place of this pane in its window."
        return as_int(self._values.get("pane_index", ""), -1)

    @property
    def window_id(self) -> str:
        'The id of the window that holds this pane: "@3".'
        return self._values.get("window_id", "")

    @property
    def active(self) -> bool:
        "True when this is the pane of its window that takes the keyboard."
        return _as_bool(self._values.get("pane_active", ""))

    @property
    def width(self) -> int:
        return as_int(self._values.get("pane_width", ""))

    @property
    def height(self) -> int:
        return as_int(self._values.get("pane_height", ""))

    @property
    def title(self) -> str:
        "The title that the program in the pane set."
        return self._values.get("pane_title", "")

    @property
    def pid(self) -> int:
        "The process id of the program in the pane."
        return as_int(self._values.get("pane_pid", ""), -1)

    @property
    def current_command(self) -> str:
        return self._values.get("pane_current_command", "")

    @property
    def current_path(self) -> str:
        return self._values.get("pane_current_path", "")

    @property
    def dead(self) -> bool:
        "True when the program of the pane has ended."
        return _as_bool(self._values.get("pane_dead", ""))

    @property
    def in_mode(self) -> bool:
        """
        True when something is drawn over the pane's program.

        **Not copy mode alone.** `#{pane_in_mode}` counts every mode
        the server draws, the clock among them, so a name saying copy
        mode said the wrong thing for one of the two.
        `mode` is which one. Lillecarl/pymux#363.
        """
        return _as_bool(self._values.get("pane_in_mode", ""))

    @property
    def mode(self) -> str:
        "What is drawn over the program: `copy-mode`, `clock-mode`, or ''."
        return self._values.get("pane_mode", "")

    @property
    def revision(self) -> int:
        """
        The revision this pane **was read at**, which is not now.

        Like every other field here it comes from the snapshot this object
        was made from, so an object a caller keeps holds the number from
        when it was made however long ago that was. For a field that
        describes a pane that is what a caller wants; for this one it is a
        trap, because the whole use of the number is comparing it with
        now. `current_revision()` asks the server, and `refresh()` reads
        every field again.

        **Compare it, never order it.** It only goes up, but a step of one
        says nothing about how much happened, and it moves on output that
        draws nothing at all -- the server counts every read from the
        program, whatever the bytes were.

        `-1` from a server too old to answer the field.
        Lillecarl/pymux#387.
        """
        return as_int(self._values.get("pane_revision", ""), -1)


class WindowFields(Record):
    _fields = WINDOW_FIELDS

    def __repr__(self) -> str:
        return "%s(%r, name=%r)" % (type(self).__name__, self.id, self.name)

    @property
    def id(self) -> str:
        'The window id, as a target: "@3".'
        return self._values["window_id"]

    @property
    def index(self) -> int:
        return as_int(self._values.get("window_index", ""), -1)

    @property
    def name(self) -> str:
        return self._values.get("window_name", "")

    @property
    def active(self) -> bool:
        "True when this is the window that the session shows."
        return _as_bool(self._values.get("window_active", ""))

    @property
    def flags(self) -> str:
        return self._values.get("window_flags", "")

    @property
    def width(self) -> int:
        return as_int(self._values.get("window_width", ""))

    @property
    def height(self) -> int:
        return as_int(self._values.get("window_height", ""))


class SessionFields(Record):
    _fields = SESSION_FIELDS

    def __repr__(self) -> str:
        return "%s(%r)" % (type(self).__name__, self.name)

    @property
    def id(self) -> str:
        'The session id, as tmux spells one: "$0".'
        return self._values.get("session_id", "$0")

    @property
    def name(self) -> str:
        return self._values.get("session_name", "")

    @property
    def attached(self) -> int:
        "How many clients are looking at this session."
        return as_int(self._values.get("session_attached", ""))

    @property
    def path(self) -> str:
        "The directory that the server started in."
        return self._values.get("session_path", "")
