"""
The sessions of another server: the rows its owner sends, and the
sessions, windows and panes a peer builds from them.

The rows are the live tables' own (`options.tables`), the ones the
snapshot and `sql` read, each sent as `{column: value}`. A column one
side does not know is skipped and a column it lacks reads as None, so
two versions of pymux still read each other's trees.

**A pane, a window and a session outlive an update; a split does not.**
A pane holds its mirror's screen, and clients, copy views and overlays
hold windows and sessions, so an update changes those in place. A
window whose shape changed gets its splits built again around the
panes it keeps. Lillecarl/pymux#759.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, cast

import anyio
from prompt_toolkit.data_structures import Size
from pyte.keep import Keep

from ..core.snapshot.read import loaded_options
from ..core.snapshot.write import session_rows
from ..core.splits import HSplit, Split, VSplit
from ..core.window_tree import LayoutTypes, Window
from ..enums import Woke
from ..ids import WindowIndex
from ..options.tables import (
    ColumnWidthRow,
    PaneRow,
    PaneTagRow,
    PaneUserVarRow,
    SessionRow,
    SessionTagRow,
    SplitRow,
    SplitWeightRow,
    WindowOptionRow,
    WindowRow,
    WindowTagRow,
)
from ..options.window_options import WindowOptions
from .program import RemoteProgram

if TYPE_CHECKING:
    from ..core.main import Pymux
    from ..core.pane import Pane
    from ..core.session import Session
    from .peer import Peer

__all__ = ["TREE_ROWS", "MirrorTree", "RemoteSession", "Tree", "read_tree", "tree_of"]

#: The tables a tree holds: what a session shows, and nothing a peer
#: cannot use, such as counters, clients or the server's options.
TREE_ROWS: tuple[type, ...] = (
    SessionRow,
    SessionTagRow,
    WindowRow,
    WindowOptionRow,
    WindowTagRow,
    ColumnWidthRow,
    SplitRow,
    SplitWeightRow,
    PaneRow,
    PaneUserVarRow,
    PaneTagRow,
)

_BY_TABLE: dict[str, type] = {kind.TABLE: kind for kind in TREE_ROWS}

#: What travels: each table's rows, as `{column: value}`.
Tree = dict[str, list[dict[str, Any]]]


def tree_of(pymux: Pymux) -> Tree:
    "Every session of `pymux`, for a peer that watches it."
    tree: Tree = {kind.TABLE: [] for kind in TREE_ROWS}
    for position, session in enumerate(pymux.sessions):
        for row in session_rows(session, position):
            rows = tree.get(row.TABLE)
            if rows is not None:
                rows.append({each.name: getattr(row, each.name) for each in fields(row)})
    return tree


def read_tree(tree: Tree) -> dict[type, list[Any]]:
    "The rows of a tree, by kind. A table or a column this build does not know is left out."
    read: dict[type, list[Any]] = {kind: [] for kind in TREE_ROWS}
    for table, rows in tree.items():
        kind = _BY_TABLE.get(table)
        if kind is None:
            continue
        columns = fields(kind)
        for row in rows:
            read[kind].append(
                kind(
                    **{
                        each.name: row.get(each.name, None if each.default is MISSING else each.default)
                        for each in columns
                    }
                )
            )
    return read


class RemoteSession(NamedTuple):
    """
    Where a mirrored session lives: the link to its owner, its id there,
    and the servers it lies behind, nearest first, by name and by id.
    """

    peer: Peer
    session_id: int
    servers: tuple[str, ...]
    server_ids: tuple[str, ...]


class MirrorTree:
    "The sessions of one peer's owner, as built here, by the owner's ids."

    KEEP: ClassVar[dict[str, Keep]] = {
        # A new link sends the whole tree again.
        "pymux": Keep.DROPPED,
        "peer": Keep.DROPPED,
        "sessions": Keep.DROPPED,
        "windows": Keep.DROPPED,
        "panes": Keep.DROPPED,
        "_shapes": Keep.DROPPED,
        "_updated": Keep.DROPPED,
    }

    def __init__(self, pymux: Pymux, peer: Peer) -> None:
        self.pymux = pymux
        self.peer = peer
        self.sessions: dict[int, Session] = {}
        self.windows: dict[int, Window] = {}
        self.panes: dict[int, Pane] = {}
        # Each window's splits and the places of its panes, as last
        # built: a window that kept its shape keeps its splits.
        self._shapes: dict[int, tuple[object, ...]] = {}
        self._updated = anyio.Event()

    def update(self, server: str, server_id: str, tree: Tree) -> None:
        """
        Make the sessions here what the owner's tree says, but for a
        session whose path holds this server: a mirror of this server's
        own, which would come back once more with every tree.
        """
        own = self.pymux.server_id
        rows = read_tree(tree)
        looped = {row.session_id for row in rows[SessionRow] if server_id == own or own in row.server_ids.split("/")}
        rows = _without_sessions(rows, looped)
        self._update_panes(rows)
        self._update_windows(rows)
        self._update_sessions(server, server_id, rows)
        self._updated.set()
        self._updated = anyio.Event()
        self.pymux.invalidate(Woke.TREE_MIRRORED)

    async def pane_once_here(self, pane_id: int, within: float) -> Pane | None:
        "The pane of that id here, once a tree brought it, or None after `within` seconds."
        with anyio.move_on_after(within):
            while pane_id not in self.panes:
                await self._updated.wait()
        return self.panes.get(pane_id)

    def id_of_window(self, window: Window) -> int | None:
        "The owner's id of a window here."
        return next((remote_id for remote_id, each in self.windows.items() if each is window), None)

    # -- panes -----------------------------------------------------------

    def _update_panes(self, rows: dict[type, list[Any]]) -> None:
        user_vars = _grouped(rows[PaneUserVarRow], "pane_id")
        tags = _grouped(rows[PaneTagRow], "pane_id")
        kept: dict[int, Pane] = {}
        for row in rows[PaneRow]:
            # An overlay floats over no window. It is not mirrored yet.
            if row.window_id is None:
                continue
            pane = self.panes.get(row.pane_id) or self._new_pane(row.pane_id)
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
            pane.user_vars = {var.name: var.value for var in user_vars.get(row.pane_id, ())}
            pane.tags = {tag.key: tag.value for tag in tags.get(row.pane_id, ())}
            cast(RemoteProgram, pane.process).command = row.current_command
            kept[row.pane_id] = pane
        for remote_id, pane in self.panes.items():
            if remote_id not in kept:
                self._drop_pane(pane)
        self.panes = kept

    def _new_pane(self, remote_id: int) -> Pane:
        pane = self.pymux.panes.build_program_pane(RemoteProgram.of(self.peer, remote_id))
        self.pymux.panes.register_pane(pane)
        return pane

    def _drop_pane(self, pane: Pane) -> None:
        "The owner's pane went: so does this one, from wherever it is here."
        pane.process.kill()
        session = self.pymux.session_holding(pane)
        if session is not None:
            session.arrangement.remove_pane(pane)

    # -- windows ---------------------------------------------------------

    def _update_windows(self, rows: dict[type, list[Any]]) -> None:
        splits = _grouped(rows[SplitRow], "window_id")
        panes = _grouped(rows[PaneRow], "window_id")
        weights = _grouped(rows[SplitWeightRow], "split_id")
        widths = _grouped(rows[ColumnWidthRow], "window_id")
        options = {row.window_id: row for row in rows[WindowOptionRow]}
        tags = _grouped(rows[WindowTagRow], "window_id")
        kept: dict[int, Window] = {}
        for row in rows[WindowRow]:
            # An unlinked window belongs to no session's list, so nobody
            # here can reach it.
            if not row.linked:
                continue
            window = self.windows.get(row.window_id) or Window(WindowIndex(row.window_index))
            window.index = WindowIndex(row.window_index)
            window.chosen_name = row.window_name
            window.zoom = bool(row.zoom)
            window.bell = bool(row.bell)
            window.activity = bool(row.activity)
            window.silence = bool(row.silence)
            window.notified = bool(row.notified)
            window.manual_size = _size(row.manual_height, row.manual_width)
            window.size_when_made = _size(row.made_height, row.made_width)
            layout = row.previous_layout
            window.previous_selected_layout = LayoutTypes(layout) if layout is not None else None
            window.tags = {tag.key: tag.value for tag in tags.get(row.window_id, ())}
            own = options.get(row.window_id)
            if own is not None:
                for attribute, value in loaded_options(WindowOptions, own).items():
                    setattr(window.options, attribute, value)

            window_splits = splits.get(row.window_id, [])
            window_panes = panes.get(row.window_id, [])
            window_weights = [weight for split in window_splits for weight in weights.get(split.split_id, ())]
            window_widths = widths.get(row.window_id, [])
            shape = (
                row.root_split_id,
                tuple(window_splits),
                tuple((pane.pane_id, pane.parent_split_id, pane.position) for pane in window_panes),
                tuple(window_weights),
                tuple(window_widths),
            )
            if self._shapes.get(row.window_id) != shape:
                self._shapes[row.window_id] = shape
                self._build_splits(window, row.root_split_id, window_splits, window_panes, weights, window_widths)
            window._active_pane = self.panes.get(cast(int, row.active_pane_id))
            kept[row.window_id] = window
        for remote_id in list(self._shapes):
            if remote_id not in kept:
                del self._shapes[remote_id]
        self.windows = kept

    def _build_splits(
        self,
        window: Window,
        root_id: int,
        split_rows: list[SplitRow],
        pane_rows: list[PaneRow],
        weights: dict[int, list[SplitWeightRow]],
        widths: list[ColumnWidthRow],
    ) -> None:
        "Build a window's splits again, in the owner's shape, around the panes here."
        child_splits = _grouped(split_rows, "parent_split_id")
        child_panes = _grouped(pane_rows, "parent_split_id")
        # The owner's keys, as this side keys the same nodes.
        keys: dict[tuple[str, int], tuple[str, int]] = {
            ("pane", remote_id): ("pane", pane.pane_id) for remote_id, pane in self.panes.items()
        }
        built: list[tuple[Split, int]] = []

        def split_of(row: SplitRow) -> Split:
            split = (HSplit if row.kind == "hsplit" else VSplit)()
            keys[("split", row.split_id)] = ("split", split.split_id)
            children: list[tuple[int, Split | Pane]] = [
                (cast(int, child.position), split_of(child)) for child in child_splits.get(row.split_id, ())
            ]
            children += [
                (cast(int, child.position), self.panes[child.pane_id])
                for child in child_panes.get(row.split_id, ())
                if child.pane_id in self.panes
            ]
            split.extend(child for _, child in sorted(children, key=lambda pair: pair[0]))
            built.append((split, row.split_id))
            return split

        root_row = next((row for row in split_rows if row.split_id == root_id), None)
        window.root = cast(HSplit | VSplit, split_of(root_row) if root_row is not None else HSplit())
        for split, remote_id in built:
            split.weights = {
                keys[(weight.kind, weight.id)]: weight.weight
                for weight in weights.get(remote_id, ())
                if (weight.kind, weight.id) in keys
            }
        window.column_widths = {
            keys[(width.kind, width.id)]: width.width for width in widths if (width.kind, width.id) in keys
        }

    # -- sessions --------------------------------------------------------

    def _update_sessions(self, server: str, server_id: str, rows: dict[type, list[Any]]) -> None:
        windows = _grouped([row for row in rows[WindowRow] if row.linked], "session_id")
        tags = _grouped(rows[SessionTagRow], "session_id")
        kept: dict[int, Session] = {}
        for row in sorted(rows[SessionRow], key=lambda row: row.position):
            session = self.sessions.get(row.session_id)
            name = "%s/%s" % (server, row.session_name)
            if session is None:
                session = self.pymux.create_session(name)
            session.remote = RemoteSession(
                self.peer,
                row.session_id,
                (server, *_path(row.server)),
                (server_id, *_path(row.server_ids)),
            )
            session.name = name
            session.tags = {tag.key: tag.value for tag in tags.get(row.session_id, ())}
            ordered = sorted(windows.get(row.session_id, ()), key=lambda window: window.position)
            session.arrangement.windows[:] = [self.windows[window.window_id] for window in ordered]
            kept[row.session_id] = session
        for remote_id, session in self.sessions.items():
            if remote_id not in kept:
                self._drop_session(session)
        self.sessions = kept

    def _drop_session(self, session: Session) -> None:
        if len(self.pymux.sessions) > 1:
            self.pymux.remove_session(session)
        else:
            # The last session stays, empty, rather than stop this
            # server: it was never this server's to end.
            session.arrangement.windows.clear()


def _path(column: str) -> tuple[str, ...]:
    "A `server` or `server_ids` column, one server for each part."
    return tuple(column.split("/")) if column else ()


def _without_sessions(rows: dict[type, list[Any]], sessions: set[int]) -> dict[type, list[Any]]:
    "The rows of a tree but those of `sessions`: their windows, splits and panes too."
    if not sessions:
        return rows
    gone: dict[str, set[int]] = {"session_id": sessions}
    gone["window_id"] = {row.window_id for row in rows[WindowRow] if row.session_id in sessions}
    gone["split_id"] = {row.split_id for row in rows[SplitRow] if row.window_id in gone["window_id"]}
    gone["pane_id"] = {row.pane_id for row in rows[PaneRow] if row.window_id in gone["window_id"]}
    return {
        kind: [row for row in each if not any(getattr(row, key, None) in ids for key, ids in gone.items())]
        for kind, each in rows.items()
    }


def _grouped(rows: list[Any], column: str) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = {}
    for row in rows:
        grouped.setdefault(getattr(row, column), []).append(row)
    return grouped


def _size(rows: int | None, columns: int | None) -> Size | None:
    return Size(rows=rows, columns=columns) if rows is not None and columns is not None else None
