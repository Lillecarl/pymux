"""
Reading a snapshot back into a fresh server, and starting the programs
it names once the server runs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast, override
from weakref import ref

import apsw
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.data_structures import Size
from prompt_toolkit.selection import SelectionType
from ptyhost.backends.posix import PosixBackend
from pyte.freeze import Frozen, thaw

from ...commands.wait_for import WaitChannel
from ...ids import PaneId, SessionId, WindowId, WindowIndex
from ...jobs.job import Job, JobId
from ...jobs.pipes import JobPipes
from ...notify.notifications import Notification, NotificationSource, Route
from ...options.client_options import ClientOptions
from ...options.option_fields import Options
from ...options.server_options import ServerOptions
from ...options.session_options import SessionOptions
from ...options.tables import (
    AppearanceRow,
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
    missing_from,
    select,
)
from ...options.window_options import WindowOptions
from ..session import Session
from ..window_tree import HSplit, LayoutTypes, Pane, VSplit, Window, _Split

if TYPE_CHECKING:
    from ..main import Pymux

from .schema import (
    CLIENT_SETTING_TYPES,
    CLIENT_SETTINGS,
    SAVED,
    SNAPSHOT_VERSION,
    STEPS,
    SnapshotError,
    panes_of,
    terminal_of,
)

__all__ = ["Inherited", "Program", "Takeover", "load", "read_server", "start"]


def _loaded_options(scope: type[Options], row: Any) -> dict[str, Any]:
    "The options a row of a scope's table holds, by attribute; a NULL is not set."
    return {
        each.attribute: each.loaded(getattr(row, each.attribute))
        for each in SAVED[scope]
        if getattr(row, each.attribute) is not None
    }


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
    return _thawed(pymux, program, pymux.panes.build_pane(backend=backend, on_done=on_done))


def _viewer(pymux: Pymux, row: ViewerRow) -> Pane:
    "A pane that views its job again. The replay draws the screen once `start` starts it."
    job = pymux.jobs.get(JobId(row.job_id))
    if job is None:
        raise SnapshotError("pane %d views job %d, which the snapshot does not hold" % (row.pane_id, row.job_id))
    pane = pymux.panes.build_viewer(job)
    pane.pane_id = PaneId(row.pane_id)
    terminal_of(pane).terminal_control.set_size(row.pane_width, row.pane_height)
    pymux.panes.register_pane(pane)
    return pane


def _thawed(pymux: Pymux, program: Program, pane: Pane) -> Pane:
    pane.pane_id = program.pane_id
    control = terminal_of(pane).terminal_control
    thaw(program.process, control.process)
    thaw(program.screen, control.screen)
    # After the screen: the replay of an open sequence reaches it.
    thaw(program.stream, control.stream)
    pymux.panes.register_pane(pane)
    return pane


async def start(pymux: Pymux) -> None:
    "Start pumping and reaping every loaded program, in the server's scope."
    if pymux.tasks is None:
        raise RuntimeError("start the loaded programs inside `Pymux.running`")
    pymux.jobs.restore()
    for job in pymux.jobs.listing():
        if not job.is_done:
            pymux.tasks.start_soon(pymux.jobs.supervise, job)
    for pane in panes_of(pymux):
        control = terminal_of(pane).terminal_control
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
    the caller's: `Serving.adopt_listener`.
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
            row.notification_id,
            row.title,
            row.body,
            row.urgency,
            pane_id,
            row.at,
            NotificationSource(row.source),
            row.seen_at,
        )
        if row.identifier is None:
            center._records.append(record)
        else:
            center._pending[pane_id, row.identifier] = record
    routes = pymux.notifications
    routes._next = counters["notification_route"]
    for row in select(db, NotificationRouteRow, "ORDER BY position"):
        route = Route(PaneId(row.pane_id), row.identifier, bool(row.reports), bool(row.focuses))
        # A notification with no identifier has no way back in from its program.
        if row.identifier:
            routes._outgoing[route.pane_id, row.identifier] = row.ours
        routes._incoming[row.ours] = route
        if row.shown:
            routes._shown[row.ours] = route.pane_id


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
        pane.current_user = row.current_user
        pane.shell = row.shell
        pane.shell_integration_version = row.shell_integration_version
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
        if row.default_height is not None and row.default_width is not None:
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
            if window_row.made_height is not None and window_row.made_width is not None:
                window.size_when_made = Size(rows=window_row.made_height, columns=window_row.made_width)
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
                lambda session=session: pymux.overlays.ended(session, overlay[0] if overlay else None),
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
                name: CLIENT_SETTING_TYPES[name](value)
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
