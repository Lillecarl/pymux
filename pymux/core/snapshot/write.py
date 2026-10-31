"""
Writing a snapshot: every row a server holds now, and the file that
holds them, written once whole and again with what changed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import apsw
from ptyhost.backends.posix import PosixBackend
from pyte.freeze import Freezer, freeze

from ...jobs.feed import JobFeed
from ...jobs.job import Job
from ...jobs.pipes import JobPipes
from ...options.client_options import ClientOptions
from ...options.option_fields import Options
from ...options.server_options import ServerOptions
from ...options.session_options import SessionOptions
from ...options.tables import (
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
    insert,
)
from ...options.window_options import WindowOptions
from ...pipes.posix import PosixSocketListener
from ..session import Session
from ..window_tree import HSplit, Pane, VSplit, Window, _Split, node_key

if TYPE_CHECKING:
    from ...connection.client_state import ClientState
    from ..main import Pymux

from .schema import CLIENT_SETTINGS, SAVED, SNAPSHOT_VERSION, SnapshotError, panes_of, terminal_of

__all__ = ["Snapshot", "fds_of", "rows_of", "save", "values_of"]


_SPLIT_KINDS: dict[type[_Split], str] = {HSplit: "hsplit", VSplit: "vsplit"}


def _value(value: Any) -> Any:
    "A value as sqlite holds it. An enum is its value; anything odd raises."
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("a snapshot cannot hold %r" % (value,))


def _stored_options(scope: type[Options], values: dict[str, Any]) -> dict[str, Any]:
    "Options by attribute, as their columns hold them."
    return {each.attribute: each.stored(values[each.attribute]) for each in SAVED[scope] if each.attribute in values}


def values_of(options: Options) -> dict[str, Any]:
    return {each.attribute: getattr(options, each.attribute) for each in SAVED[type(options)]}


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
        panes = panes_of(pymux)
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
                        fds=None if job.is_done else json.dumps(fds_of(job)),
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
        control = terminal_of(pane).terminal_control
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


def _refuse_what_cannot_be_saved(pane: Pane) -> None:
    process = terminal_of(pane).terminal_control.process
    if isinstance(process, JobFeed):
        return
    backend = getattr(process, "backend", None)
    if not isinstance(backend, PosixBackend):
        raise SnapshotError("pane %%%d runs no program on a pty" % pane.pane_id)


def fds_of(job: Job) -> list[int]:
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
        yield NotificationRow(
            *record[:6], position=position, identifier=None, source=record.source, seen_at=record.seen_at
        )
    for (_pane_id, identifier), record in center._pending.items():
        yield NotificationRow(*record[:6], position=None, identifier=identifier, source=record.source)
    for name, (sql, description) in sorted(pymux.jobs.saved_queries.items()):
        yield SavedQueryRow(name, sql, description)
    for name in sorted(pymux.jobs.forgotten_seeds):
        yield ForgottenSeedRow(name)
    for name, channel in sorted(pymux.wait_channels.items()):
        yield WaitChannelRow(name, channel.woken, channel.locked)
    routes = pymux.notifications
    for position, (ours, route) in enumerate(routes._incoming.items()):
        yield NotificationRouteRow(
            ours, route.pane_id, route.identifier, position, route.reports, route.focuses, ours in routes._shown
        )
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
    yield ServerOptionRow(1, **_stored_options(ServerOptions, values_of(pymux.options)))
    yield SessionDefaultRow(1, **_stored_options(SessionOptions, values_of(pymux.session_defaults)))
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
            default_height=session.default_size.rows if session.default_size is not None else None,
            default_width=session.default_size.columns if session.default_size is not None else None,
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
    made = window.size_when_made
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
        made_height=made.rows if made is not None else None,
        made_width=made.columns if made is not None else None,
        zoom=window.zoom,
        frame_rate_set=window.frame_rate_set,
        bell=window.bell,
        activity=window.activity,
        silence=window.silence,
        notified=window.notified,
        shown_name=window.name,
    )
    yield WindowOptionRow(window.window_id, **_stored_options(WindowOptions, values_of(window.options)))
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
        current_user=pane.current_user,
        shell=pane.shell,
        shell_integration_version=pane.shell_integration_version,
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
            "options": values_of(state.options),
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
