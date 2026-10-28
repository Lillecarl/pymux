"""
A server's window management, in an sqlite file a new build can load.

A hot upgrade writes this, and the new build reads it back
(Lillecarl/pymux#399). `sql` reads the same tables under `live`,
built from the objects as a question starts (`pymux/query.py`), so
they are shaped for a person or an agent to query: one row per
session, window, split and pane, and a column named after the
`#{format}` variable where one exists. `pymux/tables.py` holds them.

**It runs synchronously, on purpose.** The pause of an upgrade is the
event loop standing still. A writer that awaited anything would let a
pane read between two tables, and the file would describe two moments.

**Screen state belongs to the transition.** `pane_programs`,
`screen_rows` and `screen_appearances` exist only in this file,
written while handing over and read once by the new build, and so does
`client_modes`. `sql` sees the window-management tables and not these.

What each class writes is declared in `WRITES`. `tests/test_snapshot.py`
holds it against every `Keep.SAVED` in the classes' `KEEP`, so a saved
field that no table holds fails a test instead of vanishing in an
upgrade.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast, override
from weakref import ref

import apsw
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from prompt_toolkit.selection import SelectionType
from ptterm import Terminal
from ptyhost.backends.posix import PosixBackend
from ptyhost.backends.posix_utils import PtyReader
from ptyhost.process import Process
from pyte.freeze import Freezer, Frozen, freeze, saved_fields, thaw
from pyte.images import GraphicsImage, GraphicsPlacement, GraphicsState
from pyte.keep import Keep
from pyte.osc import ColorOverrides, PointerShapes
from pyte.page import CursorPosition, Page
from pyte.screen import Screen
from pyte.streams import Stream
from pyte.titles import Titles

from .arrangement import HSplit, LayoutTypes, Pane, VSplit, Window, _Split, node_key
from .client_options import ClientOptions
from .commands.wait_for import WaitChannel
from .ids import PaneId, SessionId, WindowId, WindowIndex
from .jobs import Job, JobFeed, JobId, JobPipes
from .notifications import Notification, NotificationSource
from .option_fields import Declared, Options
from .pipes.posix import PosixSocketListener
from .server_options import ServerOptions
from .session import Session
from .session_options import SessionOptions
from .tables import (
    JOB_TABLES,
    LIVE_ROWS,
    SCHEMA,
    SCREEN_TABLES,
    AppearanceRow,
    ChangedRows,
    ClientModesRow,
    ClientOptionRow,
    ClientRow,
    ClientWindowRow,
    ColumnWidthRow,
    CounterRow,
    ForgottenSeedRow,
    GlobalEnvironmentRow,
    HookRow,
    JobOutputRow,
    JobRow,
    JobTagRow,
    KeyBindingRow,
    NamedBufferRow,
    NotificationRouteRow,
    NotificationRow,
    PaneRow,
    PaneTagRow,
    PaneUserVarRow,
    ProgramRow,
    Row,
    SavedQueryRow,
    ScreenRow,
    ServerListRow,
    ServerOptionRow,
    ServerRow,
    SessionDefaultRow,
    SessionEnvironmentRow,
    SessionOptionRow,
    SessionRow,
    SessionTagRow,
    SplitRow,
    SplitWeightRow,
    ViewerRow,
    WaitChannelRow,
    WindowDefaultRow,
    WindowOptionRow,
    WindowRow,
    WindowTagRow,
    create_table,
    insert,
    literal,
    missing_from,
    select,
)
from .window_options import WindowOptions

#: The options of each scope a snapshot holds.
_SAVED: dict[type[Options], tuple[Declared, ...]] = {
    scope: tuple(each for each in scope.DECLARED.values() if each.keep is Keep.SAVED)
    for scope in (ServerOptions, SessionOptions, WindowOptions, ClientOptions)
}

if TYPE_CHECKING:
    from .main import ClientState, Pymux

__all__ = [
    "SNAPSHOT_VERSION",
    "Inherited",
    "Snapshot",
    "Takeover",
    "load",
    "save",
    "start",
]

#: The shape this build writes, in `PRAGMA user_version`. A load
#: takes an older one up through `STEPS`, and refuses any other.
SNAPSHOT_VERSION = 40


#: What a client holds besides its options, each a column of `clients`
#: and an attribute of `ClientState`, with the type sqlite gives back as.
_CLIENT_SETTING_TYPES: dict[str, type] = {
    "last_used": int,
    "read_only": bool,
    "ignore_size": bool,
    "message": str,
}
CLIENT_SETTINGS = tuple(_CLIENT_SETTING_TYPES)


#: The columns of `clients` that 39 kept a client's options in, by the
#: option's attribute where the name differs.
_OLD_CLIENT_COLUMNS = {"swap_light_and_dark_colors": "swap_dark_and_light"}


def _client_options_move_out() -> str:
    """
    39 kept a client's options as columns of `clients`, which a NULL
    left unset. 40 gives them a table of their own, and
    `swap-light-and-dark-colors` its attribute's name. Lillecarl/pymux#769.
    """
    declared = _SAVED[ClientOptions]
    old = {each.attribute: _OLD_CLIENT_COLUMNS.get(each.attribute, each.attribute) for each in declared}
    values = ",\n  ".join(
        "coalesce(%s, %s)" % (old[each.attribute], literal(each.stored(each.default))) for each in declared
    )
    return (
        create_table(ClientOptionRow)
        + "INSERT INTO client_options(client_id, %s) SELECT client_id,\n  %s\nFROM clients;\n"
        % (", ".join(each.attribute for each in declared), values)
        + "".join("ALTER TABLE clients DROP COLUMN %s;\n" % column for column in old.values())
    )


#: What takes a snapshot of the version before this one up to it. A
#: snapshot only ever goes from a running build to the next one, so a
#: change of shape adds the one step from the version before it and
#: deletes any older step. An added table or column needs none:
#: `missing_from` adds it.
STEPS: dict[int, str] = {39: _client_options_move_out()}

#: The fields `ClientState.modes` holds, one JSON value in `client_modes`.
CLIENT_MODES = frozenset(
    {
        "has_prefix",
        "key_tables",
        "confirmations",
        "prompt_text",
        "prompt_command",
        "prompt_buffer",
        "command_buffer",
        "display_popup",
        "menu_entries",
        "menu_title",
        "chooser_return_to",
        "chooser",
        "choose_window_index",
        "choose_window_filter",
        "choose_window_command",
    }
)

_SPLIT_KINDS: dict[type[_Split], str] = {HSplit: "hsplit", VSplit: "vsplit"}


#: What each class writes into a table. `tests/test_snapshot.py` says
#: this equals the class's `Keep.SAVED` fields.
WRITES: dict[str, frozenset[str]] = {
    "pymux.session.Session": frozenset(Session.KEEP),
    "pymux.arrangement.Arrangement": frozenset(
        {
            "windows",
            "_unlinked_windows",
            "window_defaults",
            "_last_active_window",
            "_active_window_for_cli",
            "_window_history_for_cli",
        }
    ),
    "pymux.arrangement.Window": frozenset(Window.KEEP)
    - {"_revisions_seen", "_quiet_since", "_silence_armed", "seen_at"},
    **{
        "%s.%s" % (scope.__module__, scope.__qualname__): frozenset(each.attribute for each in saved)
        for scope, saved in _SAVED.items()
    },
    "pymux.arrangement._Split": frozenset(_Split.KEEP),
    "pymux.arrangement.Pane": frozenset(name for name, fate in Pane.KEEP.items() if fate == Keep.SAVED),
    "pymux.main.ClientState": frozenset({"session", "previous_session", "options", *CLIENT_SETTINGS, *CLIENT_MODES}),
    # The record goes by the client's id, in `clients`.
    "pymux.server.ServerConnection": frozenset({"client_state", "created"}),
    # In `hooks`, one row per command.
    "pymux.hooks.Hooks": frozenset({"commands"}),
    "pymux.jobs.JobTable": frozenset({"_jobs", "last_id", "saved_queries", "forgotten_seeds"}),
    # The kept counts are the lengths of the chunks `job_output` holds.
    "pymux.jobs.Job": frozenset(name for name, fate in Job.KEEP.items() if fate == Keep.SAVED),
    # A viewer's row, in `pane_viewers`.
    "pymux.jobs.JobFeed": frozenset({"job", "sx", "sy"}),
    "ptterm.terminal.Terminal": frozenset({"terminal_control"}),
    "ptterm.terminal._TerminalControl": frozenset({"screen", "stream", "process"}),
    # What `pyte.freeze` walks writes every saved field, by construction.
    **{
        "%s.%s" % (cls.__module__, cls.__qualname__): frozenset(saved_fields(cls))
        for cls in (
            Screen,
            Page,
            CursorPosition,
            Titles,
            ColorOverrides,
            PointerShapes,
            GraphicsState,
            GraphicsImage,
            GraphicsPlacement,
            Stream,
            Process,
            PosixBackend,
            PtyReader,
        )
    },
    "pymux.main.Pymux": frozenset(
        {"sessions", "_session_counter", "_uses", "global_environment", "returning_clients", "jobs"}
        | {"created", "socket_name", "listener", "original_cwd", "source_file", "_startup_done"}
        | {"clipboard", "named_buffers", "prompt_history", "message_log", "hooks", "key_bindings_manager"}
        | {"notifications", "notification_center", "wait_channels"}
        | {"startup_command", "options", "session_defaults"}
    ),
}


def _value(value: Any) -> Any:
    "A value as sqlite holds it. An enum is its value; anything odd raises."
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("a snapshot cannot hold %r" % (value,))


def _like(default: Any, value: Any) -> Any:
    "A value read back, in the type of the fresh object's own default."
    if value is None:
        return None
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, Enum):
        return type(default)(value)
    return value


def _stored_options(scope: type[Options], values: dict[str, Any]) -> dict[str, Any]:
    "Options by attribute, as their columns hold them."
    return {each.attribute: each.stored(values[each.attribute]) for each in _SAVED[scope] if each.attribute in values}


def _values_of(options: Options) -> dict[str, Any]:
    return {each.attribute: getattr(options, each.attribute) for each in _SAVED[type(options)]}


def _loaded_options(scope: type[Options], row: Any) -> dict[str, Any]:
    "The options a row of a scope's table holds, by attribute; a NULL is not set."
    return {
        each.attribute: each.loaded(getattr(row, each.attribute))
        for each in _SAVED[scope]
        if getattr(row, each.attribute) is not None
    }


def _optional_id(item: Pane | HSplit | VSplit | None) -> int | None:
    return None if item is None else int(node_key(item)[1])


def save(pymux: Pymux, path: str | os.PathLike[str]) -> None:
    "Write `pymux` to `path` in one go. `Snapshot` says how."
    snapshot = Snapshot(path)
    try:
        snapshot.write(pymux)
        snapshot.finish()
    except BaseException:
        snapshot.abandon()
        raise


class Snapshot:
    """
    A snapshot being written: once whole, then again with what changed.

    An upgrade writes the first while the server still serves, which
    costs the depth of every history, and the second in the pause,
    which costs what the programs wrote in between
    (`pyte.freeze.Freezer`). `finish` then makes the file appear whole
    or not at all: it is written beside `path`, synced, and renamed
    over it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.partial = self.path.with_name(self.path.name + ".partial")
        # sqlite replays a journal it finds beside a new file into it, so
        # a crashed save's journal goes with the crashed save.
        for leftover in (self.partial, self.partial.with_name(self.partial.name + "-journal")):
            leftover.unlink(missing_ok=True)
        # Private before the first byte: the screens hold what was typed.
        # sqlite gives its journal the file's mode.
        os.close(os.open(self.partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self.db = apsw.Connection(str(self.partial))
        self.db.execute(SCHEMA)
        self.db.execute("PRAGMA user_version = %d" % SNAPSHOT_VERSION)
        self.freezers: dict[int, Freezer] = {}
        self.jobs_written: set[int] = set()
        #: The jobs the last write wrote as running.
        self.running_written: set[int] = set()
        self.rows = ChangedRows(LIVE_ROWS)
        self.modes = ChangedRows((ClientModesRow,))

    def write(self, pymux: Pymux) -> None:
        "Write what `pymux` holds now over what the write before held."
        _refuse_the_server(pymux)
        panes = _panes_of(pymux)
        for pane in panes:
            _refuse_what_cannot_be_saved(pane)
        jobs = pymux.jobs.listing()
        for job in jobs:
            if not job.is_done and (job.backend is None or job.pid is None):
                raise SnapshotError("job %d is still starting; try again" % job.job_id)
        with self.db:
            written = self.rows.write(self.db, rows_of(pymux))
            modes = self.modes.write(self.db, _client_mode_rows(pymux))
            self._write_jobs(jobs)
            alive = {pane.pane_id for pane in panes}
            for gone in set(self.freezers) - alive:
                del self.freezers[gone]
                for table in SCREEN_TABLES:
                    self.db.execute("DELETE FROM %s WHERE pane_id = ?" % table, (gone,))
            for pane in panes:
                self._write_program(pane)
        self.rows.written = written
        self.modes.written = modes

    def _write_jobs(self, jobs: list[Job]) -> None:
        db = self.db
        alive = {job.job_id for job in jobs}
        # A running job wrote more since, and its tail is capped, so it
        # is written whole again; so is one that ended since it was
        # written running, or its row would still say it runs.
        stale = {job.job_id for job in jobs if not job.is_done} | self.running_written
        for gone in self.jobs_written - (alive - stale):
            for table in JOB_TABLES:
                db.execute("DELETE FROM %s WHERE job_id = ?" % table, (gone,))
        self.jobs_written &= alive - stale
        self.running_written = {job.job_id for job in jobs if not job.is_done}
        for job in jobs:
            if job.job_id in self.jobs_written:
                continue
            self.jobs_written.add(job.job_id)
            insert(
                db,
                [
                    JobRow(
                        job_id=job.job_id,
                        command=job.command,
                        directory=job.directory,
                        pty=job.pty,
                        status=job.status,
                        returncode=job.returncode,
                        error=job.error,
                        started=job.started,
                        finished=job.finished,
                        last_active=job.last_active,
                        env=json.dumps(job.env, sort_keys=True),
                        stdout_dropped=job.stdout_dropped,
                        stderr_dropped=job.stderr_dropped,
                        pid=job.pid,
                        fds=None if job.is_done else json.dumps(_fds_of(job)),
                        parent=None if job.is_done or job.backend is None else job.backend.parent,
                    ),
                    *(JobTagRow(job.job_id, key, value) for key, value in sorted(job.tags.items())),
                    *(
                        JobOutputRow(job.job_id, stream, start, data)
                        for stream, chunks in (("stdout", job.stdout_chunks), ("stderr", job.stderr_chunks))
                        for start, data in chunks
                    ),
                ],
            )

    def _write_program(self, pane: Pane) -> None:
        control = _terminal(pane).terminal_control
        process = control.process
        if isinstance(process, JobFeed):
            # The new viewer replays the job, so the screen stays here.
            insert(self.db, [ViewerRow(pane.pane_id, process.job.job_id, process.sx, process.sy)], replace=True)
            return
        backend = process.backend
        assert isinstance(backend, PosixBackend)  # `write` refused anything else
        # A program that ended leaves no pty, and `remain-on-exit` keeps its
        # pane: it goes with no master. Lillecarl/pymux#738.
        if backend.pid is None or (backend.master is None and not backend.closed):
            raise SnapshotError("pane %%%d holds a program with no pid or no pty" % pane.pane_id)
        freezer = self.freezers.get(pane.pane_id)
        if freezer is None:
            freezer = self.freezers[pane.pane_id] = Freezer()
        screen = freezer.freeze(control.screen)
        db = self.db
        insert(
            db,
            [
                ProgramRow(
                    pane_id=pane.pane_id,
                    pane_pid=backend.pid,
                    # Output moves it, so it lives with the screen.
                    pane_revision=pane.revision,
                    screen=json.dumps(screen.root),
                    stream=json.dumps(freeze(control.stream).root),
                    process=json.dumps(freeze(process).root),
                    master_fd=backend.master,
                    parent=backend.parent,
                )
            ],
            replace=True,
        )
        first = len(freezer.appearance_index) - len(screen.appearances)
        insert(
            db,
            [
                AppearanceRow(pane.pane_id, first + index, json.dumps(value))
                for index, value in enumerate(screen.appearances)
            ],
        )
        for buffer in screen.whole:
            db.execute("DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ?", (pane.pane_id, buffer))
        for buffer, numbers in screen.removed.items():
            db.executemany(
                "DELETE FROM screen_rows WHERE pane_id = ? AND buffer = ? AND number = ?",
                [(pane.pane_id, buffer, number) for number in numbers],
            )
        insert(
            db,
            [
                ScreenRow(pane.pane_id, buffer, number, wrapped, _text_of(cells), json.dumps(cells), marks)
                for buffer, rows in screen.buffers.items()
                for number, wrapped, cells, marks in rows
            ],
            replace=True,
        )

    def finish(self) -> None:
        "Make the file appear at `path`, whole."
        self.db.close()
        with open(self.partial, "rb") as written:
            os.fsync(written.fileno())
        os.replace(self.partial, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def abandon(self) -> None:
        "Throw the half-written file away."
        self.db.close()
        self.partial.unlink(missing_ok=True)


def _panes_of(pymux: Pymux) -> list[Pane]:
    "Every pane a snapshot holds, in the order the tables write them."
    panes = []
    for session in pymux.sessions:
        arrangement = session.arrangement
        for window in [*arrangement.windows, *arrangement._unlinked_windows]:
            panes.extend(window.panes)
        if session.overlay_pane is not None:
            panes.append(session.overlay_pane)
    return panes


def _terminal(pane: Pane) -> Terminal:
    """
    The terminal of a pane the snapshot holds. A Wayland window lives on
    the server's compositor, which no handover carries, and the upgrade
    closes it before it writes. Lillecarl/pymux#620.
    """
    terminal = pane.terminal
    if not isinstance(terminal, Terminal):
        raise SnapshotError("pane %%%d shows a Wayland window, which no handover carries" % pane.pane_id)
    return terminal


def _refuse_what_cannot_be_saved(pane: Pane) -> None:
    process = _terminal(pane).terminal_control.process
    if isinstance(process, JobFeed):
        return
    backend = getattr(process, "backend", None)
    if not isinstance(backend, PosixBackend):
        raise SnapshotError("pane %%%d runs no program on a pty" % pane.pane_id)


def _fds_of(job: Job) -> list[int]:
    "What an exec carries of a running job: its master, or its two pipes."
    backend = job.backend
    if isinstance(backend, JobPipes):
        return backend.fds
    if isinstance(backend, PosixBackend) and backend.master is not None:
        return [backend.master]
    raise SnapshotError("job %d runs on nothing an exec can carry" % job.job_id)


def _text_of(cells: list[tuple[int, str, object, object]]) -> str:
    "A frozen row as text, for a question to search. A gap reads as a space."
    chars = {column: char for column, char, _, _ in cells}
    if not chars:
        return ""
    return "".join(chars.get(column, " ") for column in range(max(chars) + 1)).rstrip()


def _refuse_the_server(pymux: Pymux) -> None:
    "What no handover carries. `sql` reads such a server all the same."
    listener = pymux.listener
    if listener is not None and not isinstance(listener, PosixSocketListener):
        raise SnapshotError("the server listens on a named pipe, which no handover carries")
    for name, channel in pymux.wait_channels.items():
        # A waiter is a command connection held open in this process,
        # and this process ends: the script that waits would fail.
        if channel.waiters or channel.lockers:
            raise SnapshotError("a command waits on channel %s; signal it first" % name)


def rows_of(pymux: Pymux) -> Iterator[Row]:
    "Every row of `LIVE_TABLES` that `pymux` holds now."
    listener = pymux.listener
    clipboard = pymux.clipboard.get_data()
    for name, value in (
        ("created", pymux.created),
        ("socket_name", pymux.socket_name),
        # The new server gets it under this number (`upgrade._hand_over`).
        ("listener_fd", listener.socket.fileno() if isinstance(listener, PosixSocketListener) else None),
        ("original_cwd", pymux.original_cwd),
        ("source_file", pymux.source_file),
        ("startup_done", pymux._startup_done),
        ("clipboard", clipboard.text),
        ("clipboard_type", clipboard.type.value),
        ("startup_command", pymux.startup_command),
    ):
        yield ServerRow(name, value)
    for name, text in sorted(pymux.named_buffers.items()):
        yield NamedBufferRow(name, text)
    for position, text in enumerate(pymux.prompt_history.get_strings()):
        yield ServerListRow("prompt_history", position, text)
    for position, text in enumerate(pymux.message_log):
        yield ServerListRow("message_log", position, text)
    for hook, commands in sorted(pymux.hooks.commands.items()):
        for position, command in enumerate(commands):
            yield HookRow(hook, position, command)
    for (table, _keys), binding in sorted(
        pymux.key_bindings_manager.custom_bindings.items(), key=lambda item: (item[0][0], item[1].written)
    ):
        yield KeyBindingRow(table, binding.written, binding.command, json.dumps(binding.arguments))
    # A finished record has its place in the hub; one still assembling
    # has the identifier its chunks name instead.
    center = pymux.notification_center
    for position, record in enumerate(center._records):
        yield NotificationRow(*record[:-1], position=position, identifier=None, source=record.source)
    for (_pane_id, identifier), record in center._pending.items():
        yield NotificationRow(*record[:-1], position=None, identifier=identifier, source=record.source)
    for name, (sql, description) in sorted(pymux.jobs.saved_queries.items()):
        yield SavedQueryRow(name, sql, description)
    for name in sorted(pymux.jobs.forgotten_seeds):
        yield ForgottenSeedRow(name)
    for name, channel in sorted(pymux.wait_channels.items()):
        yield WaitChannelRow(name, channel.woken, channel.locked)
    for position, (ours, (pane_id, identifier)) in enumerate(pymux.notifications._incoming.items()):
        yield NotificationRouteRow(ours, pane_id, identifier, position)
    for name, value in (
        ("pane", Pane._pane_counter),
        ("window", Window._window_counter),
        ("split", _Split._split_counter),
        ("session", pymux._session_counter),
        ("uses", pymux._uses),
        ("job", pymux.jobs.last_id),
        ("notification", pymux.notification_center._next),
        ("notification_route", pymux.notifications._next),
    ):
        yield CounterRow(name, value)
    yield ServerOptionRow(1, **_stored_options(ServerOptions, _values_of(pymux.options)))
    yield SessionDefaultRow(1, **_stored_options(SessionOptions, _values_of(pymux.session_defaults)))
    for name, value in sorted(pymux.global_environment.items()):
        yield GlobalEnvironmentRow(name, value)

    for position, session in enumerate(pymux.sessions):
        arrangement = session.arrangement
        last = arrangement._last_active_window
        yield SessionRow(
            session_id=session.session_id,
            session_name=session.name,
            position=position,
            session_created=session.created,
            last_used=session.last_used,
            default_height=session.default_size.rows,
            default_width=session.default_size.columns,
            last_active_window_id=last.window_id if last is not None else None,
            overlay_pane_id=_optional_id(session.overlay_pane),
            overlay_title=session.overlay_title,
            overlay_width=session.overlay_width,
            overlay_height=session.overlay_height,
        )
        for name, value in sorted(session.environment.items()):
            yield SessionEnvironmentRow(session.session_id, name, value)
        for key, value in sorted(session.tags.items()):
            yield SessionTagRow(session.session_id, key, value)
        if own := session.options.own():
            yield SessionOptionRow(session.session_id, **_stored_options(SessionOptions, own))
        if arrangement.window_defaults:
            yield WindowDefaultRow(session.session_id, **_stored_options(WindowOptions, arrangement.window_defaults))
        for linked, windows in ((True, arrangement.windows), (False, arrangement._unlinked_windows)):
            for place, window in enumerate(windows):
                yield from _window_rows(session, window, linked, place)
        if session.overlay_pane is not None:
            yield from _pane_rows(session.overlay_pane, None, None, None)

    yield from _client_rows(pymux)


def _window_rows(session: Session, window: Window, linked: bool, place: int) -> Iterator[Row]:
    # The newest of the history: a restored window can go back one step.
    previous = window.previous_active_pane
    manual = window.manual_size
    yield WindowRow(
        window_id=window.window_id,
        session_id=session.session_id,
        linked=linked,
        position=place,
        window_index=int(window.index),
        window_name=window.chosen_name,
        root_split_id=window.root.split_id,
        active_pane_id=_optional_id(window._active_pane),
        previous_active_pane_id=_optional_id(previous),
        previous_layout=_value(window.previous_selected_layout),
        manual_height=manual.rows if manual is not None else None,
        manual_width=manual.columns if manual is not None else None,
        zoom=window.zoom,
        frame_rate_set=window.frame_rate_set,
        bell=window.bell,
        activity=window.activity,
        silence=window.silence,
        notified=window.notified,
        shown_name=window.name,
    )
    yield WindowOptionRow(window.window_id, **_stored_options(WindowOptions, _values_of(window.options)))
    for key, value in sorted(window.tags.items()):
        yield WindowTagRow(window.window_id, key, value)
    for (kind, id), width in sorted(window.column_widths.items()):
        yield ColumnWidthRow(window.window_id, kind, id, width)
    yield from _split_rows(window, window.root, None, None)


def _split_rows(window: Window, split: _Split, parent: _Split | None, place: int | None) -> Iterator[Row]:
    yield SplitRow(
        split_id=split.split_id,
        window_id=window.window_id,
        kind=_SPLIT_KINDS[type(split)],
        parent_split_id=parent.split_id if parent is not None else None,
        position=place,
    )
    for (kind, id), weight in sorted(split.weights.items()):
        yield SplitWeightRow(split.split_id, kind, id, weight)
    for position, child in enumerate(split):
        if isinstance(child, Pane):
            yield from _pane_rows(child, window, split, position)
        else:
            yield from _split_rows(window, child, split, position)


def _pane_rows(pane: Pane, window: Window | None, parent: _Split | None, place: int | None) -> Iterator[Row]:
    yield PaneRow(
        pane_id=pane.pane_id,
        window_id=window.window_id if window is not None else None,
        parent_split_id=parent.split_id if parent is not None else None,
        position=place,
        pane_name=pane.chosen_name,
        clock_mode=pane.clock_mode,
        pane_current_path=pane.current_directory,
        current_host=pane.current_host,
        command_zone=pane.command_zone,
        last_exit_status=pane.last_exit_status,
        bell=pane.bell,
        shown_name=pane.name,
        title=_title_of(pane),
        current_command=os.path.basename(pane.process.get_name() or "") or None,
        pane_index=window.get_pane_index(pane) if window is not None else None,
    )
    for name, value in sorted(pane.user_vars.items()):
        yield PaneUserVarRow(pane.pane_id, name, value)
    for key, value in sorted(pane.tags.items()):
        yield PaneTagRow(pane.pane_id, key, value)


def _title_of(pane: Pane) -> str | None:
    "The title the program set, where it has a screen to set one on: a Wayland pane has none."
    screen = getattr(pane.terminal, "screen", None)
    return screen.titles.window if screen is not None else None


def _clients(pymux: Pymux) -> dict[str, dict[str, Any]]:
    """
    Each client by the id it keeps across reattaches, and the windows it
    was on in each session. A client the server loaded and that has not
    come back yet is written again as it was read.
    """
    clients: dict[str, dict[str, Any]] = {key: dict(value) for key, value in pymux.returning_clients.items()}
    for connection in pymux.connections:
        state: ClientState | None = connection.client_state
        if state is None or not connection.client_id:
            continue
        windows = {}
        for session in pymux.sessions:
            arrangement = session.arrangement
            active = arrangement._active_window_for_cli.get(state.app)
            previous = arrangement.previous_window_for(state.app)
            if active is not None or previous is not None:
                windows[session.session_id] = (
                    active.window_id if active is not None else None,
                    previous.window_id if previous is not None else None,
                )
        clients[connection.client_id] = {
            "session_id": state.session.session_id if state.session is not None else None,
            "previous_session_id": state.previous_session.session_id if state.previous_session is not None else None,
            "windows": windows,
            "settings": {name: _value(getattr(state, name)) for name in CLIENT_SETTINGS},
            "options": _values_of(state.options),
            "modes": state.modes(),
            "connected": connection.created,
            "terminal_name": connection.name,
        }
    return clients


def _client_rows(pymux: Pymux) -> Iterator[Row]:
    for client_id, client in sorted(_clients(pymux).items()):
        settings = client["settings"]
        yield ClientRow(
            client_id=client_id,
            session_id=client["session_id"],
            previous_session_id=client["previous_session_id"],
            connected=client.get("connected"),
            **{name: settings.get(name) for name in CLIENT_SETTINGS},
            terminal_name=client.get("terminal_name"),
        )
        yield ClientOptionRow(client_id, **_stored_options(ClientOptions, client["options"]))
        for session_id, (active, previous) in sorted(client["windows"].items()):
            yield ClientWindowRow(client_id, session_id, active, previous)


def _client_mode_rows(pymux: Pymux) -> Iterator[ClientModesRow]:
    "What each client is in the middle of, which only the hand-over file holds."
    for client_id, client in sorted(_clients(pymux).items()):
        modes = client.get("modes")
        if modes is not None:
            yield ClientModesRow(client_id, json.dumps(modes))


class SnapshotError(Exception):
    "A snapshot this build cannot load."


class Program(NamedTuple):
    "What a snapshot holds of one pane's program, for `load` to adopt it."

    pane_id: PaneId
    row: ProgramRow
    screen: Frozen
    stream: Frozen
    process: Frozen


class Takeover:
    """
    How a load comes by the programs that still run: a pane's pty, and a
    running job's pty or pipes. Lillecarl/pymux#736.

    This one is the dry run's (`upgrade.check`): it adopts nothing and
    starts nothing, in a process that holds none of the fds. The new
    build proves it reads every table and thaws every screen. **Start
    nothing it built**: the thaw writes the old owner's pid over each pty.
    """

    def pane(self, program: Program) -> PosixBackend:
        return PosixBackend(None)

    def job(self, row: JobRow) -> PosixBackend | JobPipes | None:
        return None


class Inherited(Takeover):
    "The fds an upgrade's exec carried under the numbers the snapshot names."

    @override
    def pane(self, program: Program) -> PosixBackend:
        row = program.row
        if row.master_fd is None:
            return PosixBackend.ended(row.pane_pid)
        return PosixBackend.adopt(row.master_fd, row.pane_pid, bool(row.parent))

    @override
    def job(self, row: JobRow) -> PosixBackend | JobPipes | None:
        if row.fds is None or row.pid is None:
            raise SnapshotError("job %d runs and the snapshot names no fds for it" % (row.job_id,))
        fds = json.loads(row.fds)
        if row.pty:
            (master,) = fds
            return PosixBackend.adopt(master, row.pid, bool(row.parent))
        stdout, stderr = fds
        return JobPipes(row.pid, stdout, stderr, parent=bool(row.parent))


def _adopted(pymux: Pymux, takeover: Takeover, program: Program, on_done: Callable[[], None] | None) -> Pane:
    """
    A pane built around a program the snapshot names, which still runs.
    The screen, the parser and the process thaw over what `_build_pane`
    made. Nothing starts until `start`.
    """
    backend = takeover.pane(program)
    return _thawed(pymux, program, pymux._build_pane(backend=backend, on_done=on_done))


def _viewer(pymux: Pymux, row: ViewerRow) -> Pane:
    "A pane that views its job again. The replay draws the screen once `start` starts it."
    job = pymux.jobs.get(JobId(row.job_id))
    if job is None:
        raise SnapshotError("pane %d views job %d, which the snapshot does not hold" % (row.pane_id, row.job_id))
    pane = pymux._build_viewer(job)
    pane.pane_id = PaneId(row.pane_id)
    _terminal(pane).terminal_control.set_size(row.pane_width, row.pane_height)
    pymux._register_pane(pane)
    return pane


def _thawed(pymux: Pymux, program: Program, pane: Pane) -> Pane:
    pane.pane_id = program.pane_id
    control = _terminal(pane).terminal_control
    thaw(program.process, control.process)
    thaw(program.screen, control.screen)
    # After the screen: the replay of an open sequence reaches it.
    thaw(program.stream, control.stream)
    pymux._register_pane(pane)
    return pane


async def start(pymux: Pymux) -> None:
    "Start pumping and reaping every loaded program, in the server's scope."
    if pymux.tasks is None:
        raise RuntimeError("start the loaded programs inside `Pymux.running`")
    pymux.jobs.restore()
    for job in pymux.jobs.listing():
        if not job.is_done:
            pymux.tasks.start_soon(pymux.jobs.supervise, job)
    for pane in _panes_of(pymux):
        control = _terminal(pane).terminal_control
        if not control._running:
            await control.start(pymux.tasks)


def _open(path: str | os.PathLike[str]) -> apsw.Connection:
    "The snapshot at `path`, read only, refused if another build wrote it."
    db = apsw.Connection(str(Path(path).resolve()), flags=apsw.SQLITE_OPEN_READONLY)
    version = int(db.pragma("user_version"))
    if version == SNAPSHOT_VERSION:
        added = missing_from(db)
        if not added:
            return db
        steps = [added]
    elif version == SNAPSHOT_VERSION - 1 and version in STEPS:
        steps = [STEPS[version]]
    else:
        db.close()
        raise SnapshotError("snapshot version %d, and this build reads %d" % (version, SNAPSHOT_VERSION))
    # The file stays as the old server wrote it: it serves on if this fails.
    stepped = apsw.Connection(":memory:")
    # Not `with`: apsw 3.54.1.0's stub types `Backup.__exit__` so that
    # no checker accepts it.
    backup = stepped.backup("main", db, "main")
    try:
        backup.step()
    finally:
        backup.finish()
    db.close()
    for step in steps:
        stepped.execute(step)
    return stepped


def read_server(path: str | os.PathLike[str]) -> dict[str, Any]:
    "The `server` table alone, for what has to be known before `load`."
    db = _open(path)
    try:
        return {row.name: row.value for row in select(db, ServerRow)}
    finally:
        db.close()


def load(pymux: Pymux, path: str | os.PathLike[str], takeover: Takeover | None = None) -> int | None:
    """
    Replace what a fresh `pymux` holds with a snapshot's.

    `takeover` says where each running program's fds come from:
    `Inherited` after an upgrade's exec. The default adopts nothing, for
    the dry run.

    Returns the fd of the listening socket, or None when the server had
    none. An exec carries it under the same number, and adopting it is
    the caller's: `Pymux.adopt_listener`.
    """
    db = _open(path)
    try:
        return _read(pymux, db, takeover or Takeover())
    finally:
        db.close()


def _program(db: apsw.Connection, pane_id: PaneId) -> Program:
    (row,) = select(db, ProgramRow, "WHERE pane_id = ?", pane_id)
    appearances = [
        json.loads(entry.value) for entry in select(db, AppearanceRow, "WHERE pane_id = ? ORDER BY appearance", pane_id)
    ]
    buffers: dict[str, list[list[Any]]] = {}
    for entry in select(db, ScreenRow, "WHERE pane_id = ? ORDER BY buffer, number", pane_id):
        buffers.setdefault(entry.buffer, []).append(
            [entry.number, bool(entry.wrapped), json.loads(entry.cells), entry.marks]
        )
    return Program(
        pane_id=pane_id,
        row=row,
        screen=Frozen(json.loads(row.screen), buffers, appearances, sorted(buffers), {}),
        stream=Frozen(json.loads(row.stream), {}, [], [], {}),
        process=Frozen(json.loads(row.process), {}, [], [], {}),
    )


def _read_bindings(pymux: Pymux, db: apsw.Connection) -> None:
    """
    The hooks and key bindings, whole, over what the configuration set:
    a person's `bind-key` and `set-hook` since the server started are in
    these and not in it.
    """
    pymux.hooks.commands = {}
    for row in select(db, HookRow, "ORDER BY hook, position"):
        pymux.hooks.add(row.hook, row.command)
    bindings = pymux.key_bindings_manager
    for table, keys in list(bindings.custom_bindings):
        bindings.remove_custom_binding(bindings.custom_bindings[table, keys].written, table=table)
    for row in select(db, KeyBindingRow):
        bindings.add_custom_binding(row.written, row.command, json.loads(row.arguments), table=row.key_table)


def _read_notifications(pymux: Pymux, db: apsw.Connection, counters: dict[str, int]) -> None:
    "The hub and its routes."
    center = pymux.notification_center
    center._next = counters["notification"]
    for row in select(db, NotificationRow, "ORDER BY position"):
        pane_id = PaneId(row.pane_id) if row.pane_id is not None else None
        record = Notification(
            row.notification_id, row.title, row.body, row.urgency, pane_id, row.at, NotificationSource(row.source)
        )
        if row.identifier is None:
            center._records.append(record)
        else:
            center._pending[pane_id, row.identifier] = record
    routes = pymux.notifications
    routes._next = counters["notification_route"]
    for row in select(db, NotificationRouteRow, "ORDER BY position"):
        key = (PaneId(row.pane_id), row.identifier)
        routes._outgoing[key] = row.ours
        routes._incoming[row.ours] = key


def _read(pymux: Pymux, db: apsw.Connection, takeover: Takeover) -> int | None:
    server = {row.name: row.value for row in select(db, ServerRow)}
    pymux.created = server["created"]
    pymux.socket_name = server["socket_name"]
    pymux.original_cwd = server["original_cwd"]
    pymux.source_file = server["source_file"]
    pymux._startup_done = bool(server["startup_done"])
    pymux.clipboard.set_data(ClipboardData(server["clipboard"], SelectionType(server["clipboard_type"])))
    pymux.named_buffers = {row.name: row.text for row in select(db, NamedBufferRow)}
    for row in select(db, ServerListRow, "WHERE list = 'prompt_history' ORDER BY position"):
        pymux.prompt_history.append_string(row.text)
    pymux.message_log.extend(
        row.text for row in select(db, ServerListRow, "WHERE list = 'message_log' ORDER BY position")
    )
    _read_bindings(pymux, db)
    pymux.startup_command = server["startup_command"]
    pymux.wait_channels = {}
    for row in select(db, WaitChannelRow):
        channel = WaitChannel()
        channel.woken = bool(row.woken)
        channel.locked = bool(row.locked)
        pymux.wait_channels[row.name] = channel

    counters = {row.name: row.value for row in select(db, CounterRow)}

    for row in select(db, ServerOptionRow):
        for attribute, value in _loaded_options(ServerOptions, row).items():
            setattr(pymux.options, attribute, value)
    for row in select(db, SessionDefaultRow):
        for attribute, value in _loaded_options(SessionOptions, row).items():
            setattr(pymux.session_defaults, attribute, value)
    pymux.global_environment = {row.name: row.value for row in select(db, GlobalEnvironmentRow)}

    panes: dict[int | None, Pane] = {}

    # Before the panes: a viewer follows its job.
    for job in _read_jobs(db, takeover):
        pymux.jobs._jobs[job.job_id] = job

    def pane_of(row: PaneRow, on_done: Callable[[], None] | None = None) -> Pane:
        viewing = select(db, ViewerRow, "WHERE pane_id = ?", row.pane_id)
        if viewing:
            pane = _viewer(pymux, viewing[0])
        else:
            program = _program(db, PaneId(row.pane_id))
            pane = _adopted(pymux, takeover, program, on_done)
            pane.revision = program.row.pane_revision
        pane.pane_id = PaneId(row.pane_id)
        pane.chosen_name = row.pane_name
        pane.clock_mode = bool(row.clock_mode)
        pane.current_directory = row.pane_current_path
        pane.current_host = row.current_host
        pane.command_zone = row.command_zone
        pane.last_exit_status = row.last_exit_status
        pane.bell = bool(row.bell)
        pane.user_vars = {var.name: var.value for var in select(db, PaneUserVarRow, "WHERE pane_id = ?", pane.pane_id)}
        pane.tags = {tag.key: tag.value for tag in select(db, PaneTagRow, "WHERE pane_id = ?", pane.pane_id)}
        panes[pane.pane_id] = pane
        pymux.panes_by_id[pane.pane_id] = pane
        return pane

    def split_of(row: SplitRow) -> _Split:
        split = (HSplit if row.kind == "hsplit" else VSplit)()
        split.split_id = row.split_id
        split.weights = {
            (weight.kind, weight.id): weight.weight
            for weight in select(db, SplitWeightRow, "WHERE split_id = ?", split.split_id)
        }
        children = [
            *[(r.position, split_of(r)) for r in select(db, SplitRow, "WHERE parent_split_id = ?", split.split_id)],
            *[(r.position, pane_of(r)) for r in select(db, PaneRow, "WHERE parent_split_id = ?", split.split_id)],
        ]
        # Rows with a parent always carry a position: the writer
        # numbers every child and leaves None only for parentless
        # rows, which this query excludes.
        split.extend(child for _, child in sorted(children, key=lambda pair: cast(int, pair[0])))
        return split

    windows: dict[int, Window] = {}
    sessions = []
    for row in select(db, SessionRow, "ORDER BY position"):
        session = Session(SessionId(row.session_id), row.session_name, defaults=pymux.session_defaults)
        session.created = row.session_created
        session.last_used = row.last_used
        session.default_size = Size(rows=row.default_height, columns=row.default_width)
        session.overlay_title = row.overlay_title
        session.overlay_width = row.overlay_width
        session.overlay_height = row.overlay_height
        session.environment = {
            env.name: env.value for env in select(db, SessionEnvironmentRow, "WHERE session_id = ?", session.session_id)
        }
        session.tags = {
            tag.key: tag.value for tag in select(db, SessionTagRow, "WHERE session_id = ?", session.session_id)
        }
        for own in select(db, SessionOptionRow, "WHERE session_id = ?", session.session_id):
            for attribute, value in _loaded_options(SessionOptions, own).items():
                setattr(session.options, attribute, value)
        arrangement = session.arrangement
        for default in select(db, WindowDefaultRow, "WHERE session_id = ?", session.session_id):
            arrangement.window_defaults.update(_loaded_options(WindowOptions, default))

        for window_row in select(
            db, WindowRow, "WHERE session_id = ? ORDER BY linked DESC, position", session.session_id
        ):
            window = Window(WindowIndex(window_row.window_index))
            window.window_id = WindowId(window_row.window_id)
            window.chosen_name = window_row.window_name
            window.tags = {
                tag.key: tag.value for tag in select(db, WindowTagRow, "WHERE window_id = ?", window.window_id)
            }
            (root_row,) = select(db, SplitRow, "WHERE split_id = ?", window_row.root_split_id)
            window.root = split_of(root_row)  # type: ignore[assignment]
            window._active_pane = panes.get(window_row.active_pane_id)
            previous = panes.get(window_row.previous_active_pane_id)
            window._pane_history = [ref(previous)] if previous is not None else []
            layout = window_row.previous_layout
            window.previous_selected_layout = LayoutTypes(layout) if layout is not None else None
            for own in select(db, WindowOptionRow, "WHERE window_id = ?", window.window_id):
                for attribute, value in _loaded_options(WindowOptions, own).items():
                    setattr(window.options, attribute, value)
            if window_row.manual_height is not None and window_row.manual_width is not None:
                window.manual_size = Size(rows=window_row.manual_height, columns=window_row.manual_width)
            window.zoom = bool(window_row.zoom)
            window.frame_rate_set = bool(window_row.frame_rate_set)
            window.bell = bool(window_row.bell)
            window.activity = bool(window_row.activity)
            window.silence = bool(window_row.silence)
            window.notified = bool(window_row.notified)
            window.column_widths = {
                (width.kind, width.id): width.width
                for width in select(db, ColumnWidthRow, "WHERE window_id = ?", window.window_id)
            }
            (arrangement.windows if window_row.linked else arrangement._unlinked_windows).append(window)
            windows[window.window_id] = window

        arrangement._last_active_window = windows.get(cast(int, row.last_active_window_id))
        if row.overlay_pane_id is not None:
            (overlay_row,) = select(db, PaneRow, "WHERE pane_id = ?", row.overlay_pane_id)
            overlay: list[Pane] = []
            session.overlay_pane = pane_of(
                overlay_row,
                lambda session=session: pymux.overlay_ended(session, overlay[0] if overlay else None),
            )
            overlay.append(session.overlay_pane)
        sessions.append(session)

    pymux.sessions = sessions

    pymux.returning_clients = {}
    modes = {row.client_id: json.loads(row.modes) for row in select(db, ClientModesRow)}
    for client in select(db, ClientRow):
        pymux.returning_clients[client.client_id] = {
            "session_id": client.session_id,
            "previous_session_id": client.previous_session_id,
            "windows": {
                window.session_id: (window.active_window_id, window.previous_window_id)
                for window in select(db, ClientWindowRow, "WHERE client_id = ?", client.client_id)
            },
            "settings": {
                name: _CLIENT_SETTING_TYPES[name](value)
                for name in CLIENT_SETTINGS
                if (value := getattr(client, name)) is not None
            },
            "options": {
                attribute: value
                for row in select(db, ClientOptionRow, "WHERE client_id = ?", client.client_id)
                for attribute, value in _loaded_options(ClientOptions, row).items()
            },
            "modes": modes.get(client.client_id),
            "connected": client.connected,
        }

    Pane._pane_counter = counters["pane"]
    Window._window_counter = counters["window"]
    _Split._split_counter = counters["split"]
    pymux._session_counter = counters["session"]
    pymux._uses = counters["uses"]
    pymux.jobs.last_id = counters["job"]
    pymux.jobs.saved_queries = {row.name: (row.sql, row.description) for row in select(db, SavedQueryRow)}
    pymux.jobs.forgotten_seeds = {row.name for row in select(db, ForgottenSeedRow)}
    _read_notifications(pymux, db, counters)
    return server["listener_fd"]


def _read_jobs(db: apsw.Connection, takeover: Takeover) -> list[Job]:
    """
    The jobs, for `start` to write into the jobs database. A running one
    takes its terminal or its pipes from `takeover`, and `start`
    supervises it again.
    """
    jobs = []
    for row in select(db, JobRow, "ORDER BY job_id"):
        job = Job(
            JobId(row.job_id),
            row.command,
            row.directory,
            row.started,
            [(tag.key, tag.value) for tag in select(db, JobTagRow, "WHERE job_id = ?", row.job_id)],
            json.loads(row.env),
            bool(row.pty),
        )
        job.status = row.status
        job.returncode = row.returncode
        job.error = row.error
        job.finished = row.finished
        job.last_active = row.last_active
        job.stdout_dropped = row.stdout_dropped
        job.stderr_dropped = row.stderr_dropped
        for chunk in select(db, JobOutputRow, "WHERE job_id = ? ORDER BY stream, start", job.job_id):
            chunks = job.stdout_chunks if chunk.stream == "stdout" else job.stderr_chunks
            chunks.append((chunk.start, chunk.data))
        job.stdout_kept = sum(len(data) for _, data in job.stdout_chunks)
        job.stderr_kept = sum(len(data) for _, data in job.stderr_chunks)
        job.pid = row.pid
        if not job.is_done:
            job.backend = takeover.job(row)
        jobs.append(job)
    return jobs
