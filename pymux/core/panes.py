"""
How a pane is made and how it ends: the process or job behind it, the
terminal widget around that, and the window pane a Wayland program
opens. `Pymux` keeps the registry of panes; this builds what goes in it.
"""

from __future__ import annotations

import os
import shlex
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

import anyio
import anyio.abc
from prompt_toolkit.application.current import get_app
from ptterm import Terminal
from ptyhost.backends import Backend
from ptyhost.backends.posix import PosixBackend
from pyte.keep import Keep

from .. import hacks_and_lies
from ..commands import CommandException
from ..enums import EditingKeys, RenderMode, Woke
from ..ids import PaneId, WindowIndex
from ..jobs.feed import JobFeed
from ..jobs.job import Job
from ..keys.actions import Kind, Origin, note, note_keys, started_by
from ..keys.key_spelling import why_pane_cannot_read
from ..log import logger
from ..wayland.window_pane import WindowWidget, is_window_pane
from .environment import pane_environment
from .pane import Pane
from .planes import remember_the_size_made_at
from .session import Session
from .window_tree import Window

if TYPE_CHECKING:
    from pyte.keys.events import KeyEvent

    from .main import Pymux

__all__ = ["MAX_KEYS_TO_REMEMBER", "PaneMaker"]

#: How many keys one pane remembers saying it could not read. A held
#: key repeats, and one line per repeat is a log nobody reads. A
#: keyboard has fewer keys than this.
MAX_KEYS_TO_REMEMBER = 512


def _say_key_did_not_fit():
    """
    A reporter for one pane, for a key it reads as something else.

    **Once per key, not once per repeat.** The same shape
    `KittyVt100Parser._say_it_has_no_name` uses for a key pymux itself
    cannot name, so the two failures read alike. Lillecarl/pymux#238.
    """
    already_said: set[KeyEvent] = set()

    def say(event: KeyEvent, lost: int, encoded: str) -> None:
        if event in already_said:
            return
        if len(already_said) < MAX_KEYS_TO_REMEMBER:
            already_said.add(event)
        logger.info("%s", why_pane_cannot_read(event, lost, encoded))

    return say


class PaneMaker:
    "Builds each pane and window, registers it, and takes it down when it ends or a person kills it."

    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        "hanging_up": Keep.DROPPED,  # an upgrade kills them before it goes
        "_killed_in": Keep.DROPPED,  # the panes it names are hanging up, and go with them
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        #: The session each pane `kill_pane` took out was in, until its
        #: program ends: by then no session holds it, and the end has to
        #: know which one it may leave empty. Lillecarl/pymux#807.
        self._killed_in: dict[PaneId, Session] = {}
        #: Programs sent SIGHUP whose SIGKILL may still be due. The
        #: grace task keeps each backend alive until the program ends.
        self.hanging_up: weakref.WeakSet[PosixBackend] = weakref.WeakSet()

    async def create_pane(
        self,
        window: Window | None = None,
        command: str | None = None,
        start_directory: str | None = None,
        on_done: Callable[[], None] | None = None,
        session: Session | None = None,
        job: Job | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
    ):
        """
        Create a new :class:`pymux.core.pane.Pane` instance. (Don't put it in
        a window yet.)

        :param window: If a window is given, take the CWD of the current
            process of that window as the start path for this pane.
        :param command: If given, run this command instead of `default-shell`.
        :param start_directory: If given, use this as the CWD.
        :param session: Where this pane is going, which says how big it
            is until a client renders it. The pane is not in a window
            yet, so it cannot be asked.
        :param job: If given, show this job instead of running anything:
            a viewer pane, read-only, that replays the tail and follows
            the rest. Nothing forks and no program runs.
        :param task_group: Whose scope the program runs in. Without one
            it is the server's, and a pane made outside `Pymux.running`
            names its own: tests hold one for the test. Nothing is
            started without a group to watch it.
        """
        if task_group is None:
            task_group = self.pymux.tasks
        if task_group is None:
            raise RuntimeError(
                "A pane was made outside `Pymux.running` with no task group, so nothing would watch its program."
            )

        # Start directory.
        path: str | None

        path = start_directory or self.directory_to_start_in(window, session)
        options = (session or self.pymux.current_session).options

        def environment_of_the_pane(environment: dict[str, str]) -> None:
            # A pane is not the terminal that the client attached
            # from: it answers the protocol queries for itself. ptterm
            # has already said so here, because it is the layer that
            # owns both the screen and the process
            # (Lillecarl/pymux#125). What is left is what only pymux
            # knows.
            #
            # The name is one of those. `default-terminal` is an option
            # a person can set, and this hook runs after ptterm's, so
            # what they set wins.
            environment["TERM"] = self.pymux.options.default_terminal

            if self.pymux.socket_name:
                environment["PYMUX"] = "%s,%i" % (self.pymux.socket_name, pane.pane_id)

            # The environment a `set-environment` filled. The global
            # scope lands first and the session's over it; a name
            # either unset takes itself out of the child's
            # environment, which is the one thing the child would
            # otherwise inherit from the server. Lillecarl/pymux#270.
            # Merged onto the copy and not onto os.environ, which still
            # holds what ptterm's edit above took out of the pane.
            # The sound names go first, so a `set-environment` of the
            # same name wins. Lillecarl/pymux#657.
            self.pymux.audio.pane_environment(environment)
            merged = pane_environment(self.pymux, environment)
            environment.clear()
            environment.update(merged)

            # The shims, when they are on, reach the opener and the
            # notifier of this session before anything else on the PATH
            # of the pane, and name the opener for what reads $BROWSER.
            self.pymux.shims.put_on_path(environment, options)

            # Our compositor only where the session names no display:
            # a program started beside a desktop belongs on it.
            if self.pymux.wayland.display and not environment.get("WAYLAND_DISPLAY"):
                environment["WAYLAND_DISPLAY"] = self.pymux.wayland.display

        # `shlex.split` and not `str.split`: a command reaches this as
        # one string, and the quoting inside it is what says where one
        # argument ends. A plain split on whitespace tears
        # `sh -c 'echo one two'` into seven words, and `sh` then reads
        # `'echo` as the whole of its script.
        #
        # `run_pymux.run` builds the string with `shlex.quote`, so this
        # undoes exactly what that did.
        command_list = shlex.split(command) if command else [options.default_shell]

        self.pymux.shims.make(options)

        if job is not None:
            pane = self.build_viewer(job)
        else:
            pane = self.build_pane(
                command=command_list,
                environment=environment_of_the_pane,
                directory=path or self.pymux.original_cwd,
                on_done=on_done,
            )
        assert isinstance(pane.terminal, Terminal), "`_build_pane` builds a terminal pane"
        terminal_control = pane.terminal.terminal_control

        if not terminal_control._running:
            # The size of the session this pane is going into, until a
            # client attaches. `new-session -x -y` is what names it, and
            # a pane no client ever renders keeps it for ever, which is
            # what an automated caller works on. Lillecarl/pymux#459.
            #
            # **The control and not the process.** `Process.set_size` tells
            # the pty and nothing else; `TerminalControl.set_size` tells the
            # pty and the screen. A screen starts at nought by nought and is
            # sized by the first render, which a detached pane never gets, so
            # sizing only the pty left the screen at zero columns: every
            # character a program wrote wrapped onto a row of its own.
            #
            # It showed as `capture-pane -p` printing one character per line,
            # while `-J` read correctly because it joins wrapped rows back
            # together. Lillecarl/pymux#321.
            size = self.pymux.planes.size_with_no_client(session, window)
            terminal_control.set_size(size.columns, size.rows)
            # A program starts watched, so that panes in detached
            # sessions also run and produce output. (Like tmux does.) A
            # viewer's feed starts the same way and follows the job.
            await terminal_control.start(task_group)

        self.register_pane(pane)

        if job is not None:
            logger.info("Viewing job %d in a new pane.", job.job_id)
        else:
            logger.info("Created process %r.", command_list)

        return pane

    def build_viewer(self, job: Job) -> Pane:
        """
        A pane that shows `job` where a program would run, with nothing
        started. The feed replays the tail and follows the rest once it
        starts. Nothing forks.
        """
        pane = self.build_pane(
            command=[self.pymux.session_defaults.default_shell],
            # A viewer never ends on its own: the job ending feeds an
            # exit line and the pane stays, so there is nothing to call
            # back. `JobFeed.kill` stops the follow; whoever kills
            # removes the pane.
            watch_end=False,
        )
        assert isinstance(pane.terminal, Terminal), "`_build_pane` builds a terminal pane"
        terminal_control = pane.terminal.terminal_control
        old_process = terminal_control.process
        terminal_control.process = JobFeed(job, terminal_control.feed_output)
        # No fork, so the pty the construction opened is dead weight:
        # give both ends back. The reader never connected, so `close`
        # only drops the master; the slave would otherwise stay open
        # until the server exits, which closes it only on a reap that
        # never comes.
        backend = old_process.backend
        if isinstance(backend, PosixBackend):
            backend.close()
            if backend.slave is not None:
                os.close(backend.slave)
                backend.slave = None
        return pane

    def _outer_term(self) -> str:
        "The TERM of the terminal the newest client sits at, as `hacks_and_lies.outer_term` reads it."
        client = self.pymux.latest_client()
        return hacks_and_lies.outer_term(client.connection.colors.term if client is not None else "")

    def build_pane(
        self,
        backend: Backend | None = None,
        command: list[str] | None = None,
        environment: Callable[[dict[str, str]], None] | None = None,
        directory: str | None = None,
        on_done: Callable[[], None] | None = None,
        watch_end: bool = True,
    ) -> Pane:
        """
        A pane and its widget, wired to this server, with nothing started.

        `backend` is the program's pty: a new one from `command` when
        none is given, or one a snapshot's load adopted. Building is
        synchronous, so a load builds every pane before anything runs.
        Lillecarl/pymux#399.
        """

        def done_callback():
            "When the process finishes."
            if on_done is not None:
                on_done()

            self.pymux.finish_recordings([pane])

            if not self.pymux.options_of_pane(pane).remain_on_exit:
                self.take_ended_pane(pane)

            self.pymux.invalidate(Woke.PANE_ENDED)

        def bell():
            "Sound bell on all clients, and flag it where nobody saw it."
            place = self.pymux._place_of(pane)
            if (self.pymux.session_defaults if place is None else place[0].options).bell:
                for c in self.pymux.apps:
                    c.output.bell()
            if place is not None:
                session, window = place
                if session.arrangement.ring(window, pane, self.pymux.apps):
                    self.pymux.invalidate(Woke.PANE_RANG)

        def forward_osc(code: str, param: str) -> None:
            "Pass an OSC sequence of this pane to the terminals of the clients."
            self.pymux.outward.forward_osc(pane, code, param)

        def resize(lines: int | None, columns: int | None) -> None:
            "Give a pane the size that the program inside it asks for."
            self.pymux.planes.resize_pane_for_program(pane, lines, columns)

        def may_resize() -> bool:
            """
            Would such an ask be granted?

            Read every time, because a person can turn the option on and
            off while the pane runs. A pane that answers "yes" and then
            gives no room sends a program to lay its output out for a
            width it does not have.
            """
            return self.pymux.options_of_pane(pane).allow_program_resize

        def may_type() -> bool:
            """
            May the person whose key this is drive the pane?

            A pane is shared and a keyboard is not, so this asks about
            the client the key came from and never about the pane. The
            answer is no for a client that attached with
            `attach-session -r`. Lillecarl/pymux#467.

            **No client is yes.** A key with no client behind it comes
            from a test, which has nobody to refuse.
            """
            try:
                return not self.pymux.get_client_state().read_only
            except ValueError:
                return True

        def takes_key_first() -> bool:
            "A client that holds the prefix: the next key is pymux's."
            try:
                return self.pymux.get_client_state().has_prefix
            except ValueError:
                return False

        # Create new pane and terminal.
        terminal = Terminal(
            done_callback=done_callback if watch_end else None,
            backend=backend,
            bell_func=bell,
            osc_func=forward_osc,
            # What copy mode copied. The pane does not build the
            # sequence for it: the option that says whether it goes
            # out lives here, and so does the check every payload
            # passes. Lillecarl/pymux#378.
            copy_func=self.pymux.outward.copied_in_copy_mode,
            resize_func=resize,
            may_resize=may_resize,
            may_type=may_type,
            takes_key_first=takes_key_first,
            environment=environment,
            directory=directory,
            command=command,
            # The `history-limit` option, which said how far copy mode
            # could scroll and never reached the screen that holds the
            # rows. A pane kept two thousand whatever the option said.
            get_history_limit=lambda: self.pymux.options_of_pane(pane).history_limit,
            unreadable_key_func=_say_key_did_not_fit(),
            key_profile_func=lambda: self.pymux.key_profile_of(self.pymux.client_of(get_app())),
            helix_keys=lambda: self.pymux.options_of_pane(pane).mode_keys is EditingKeys.HELIX,
            wheel_lines=lambda: self.pymux.options_of_pane(pane).copy_mode_wheel_lines,
        )
        terminal.on_copy_command = lambda keys: note_keys(self.pymux, "copy-mode", keys)
        pane = Pane(terminal)
        # The name the pane answers XTVERSION with, asked at each query:
        # the program in front may be one `hacks_and_lies` lies to.
        terminal.screen.version = lambda: hacks_and_lies.version(pane.process.get_name(), self._outer_term())

        terminal_control = terminal.terminal_control
        terminal_control.keep_rows = self.pymux.options.render_mode is RenderMode.OPTIMIZED

        # What moves `#{pane_revision}`. It is wired here and not in
        # `Pane`, because a pane is about the arrangement and knows
        # nothing about a widget: a dozen tests build one around a stub
        # that has no terminal control at all. `ptyhost` fires this after
        # every read from the program, whatever the bytes were.
        # Lillecarl/pymux#387.
        def changed(_sender: object) -> None:
            pane.content_changed()

        terminal_control.on_content_changed += changed

        def focus_pane_on_click() -> None:
            """
            Select this pane: the mouse focused it.

            The widget moves the layout focus on a click in a pane
            that had none; the arrangement follows here, or the next
            keypress syncs the layout focus straight back to the pane
            that is active. A pane in no window -- an overlay, or one
            already gone -- stays as it is. Lillecarl/pymux#527.
            """
            found = self.pymux.place_of_pane_id(pane.pane_id)
            if found is None:
                return
            _, window, _ = found
            window.active_pane = pane
            with started_by(Origin.MOUSE):
                note(self.pymux, Kind.CLICK, "select-pane")

        # What a click selects. It is wired here and not in `Pane`,
        # for the reason `on_content_changed` gives above: a pane is
        # about the arrangement and knows nothing about a widget.
        terminal_control.on_mouse_focus = focus_pane_on_click
        terminal_control.placeholder_ids = lambda image_id: self.pymux.outer_image_ids.id_of(pane.pane_id, image_id)

        return pane

    def take_ended_pane(self, pane: Pane) -> None:
        "Take a pane whose program ended out of its session, and the session if it empties."
        # The session the pane was in, and not the session of whoever
        # happens to be the current client when a program ends.
        killed_in = self._killed_in.pop(pane.pane_id, None)
        session = self.pymux.session_holding(pane) or killed_in
        if session is not None:
            session.arrangement.remove_pane(pane)
            self.end_if_empty(session)

        # Make sure the right pane is focused for each client.
        for client_state in self.pymux._client_states.values():
            client_state.sync_focus()

    def end_if_empty(self, session: Session) -> None:
        "A session with no panes left is over. The last one to go stops the server."
        if session in self.pymux.sessions and not session.arrangement.has_panes:
            self.pymux.remove_session(session)

    def open_window_pane(self, widget: WindowWidget, beside: Pane | None = None) -> Pane | None:
        """
        A pane for a Wayland window that just opened, split off in the
        window that holds `beside`, or else beside the active pane of the
        session a person used last. None when there is no window to put
        it in. Lillecarl/pymux#620.
        """
        found = None if beside is None else self.pymux.place_of_pane_id(beside.pane_id)
        if found is not None:
            window = found[1]
        elif self.pymux.sessions:
            window = self.pymux.last_used_session.arrangement.get_active_window()
        else:
            return None
        if window is None:
            return None

        pane = self._window_pane_of(widget)
        if beside is not None:
            # One program, one set of tags: a caller finds all its windows by them.
            pane.tags.update(beside.tags)
        window.add_pane(pane, vsplit=True)
        self._window_pane_shown()
        return pane

    def adopt_window_pane(self, old: Pane, widget: WindowWidget) -> Pane | None:
        """
        A pane for a Wayland window, in the place of `old`, whose program
        is sent SIGHUP: `pymux wl` turns the pane it ran in into the
        window. None when `old` is gone. Lillecarl/pymux#630.

        SIGHUP and not SIGKILL, so the shell writes the `pymux wl` line
        to its history on the way out.
        """
        session = self.pymux.session_holding(old)
        if session is None:
            return None
        pane = self._window_pane_of(widget)
        pane.tags.update(old.tags)
        # The swap first: an ended program takes its pane out of the
        # session, and a session left with none stops the server.
        session.arrangement.replace_pane(old, pane)
        self.hang_up(old)
        self._window_pane_shown()
        return pane

    def _window_pane_of(self, widget: WindowWidget) -> Pane:
        pane = Pane(widget)

        def focus_pane_on_click() -> None:
            "Select this pane: the mouse went to it, as `_build_pane` does for a terminal."
            found = self.pymux.place_of_pane_id(pane.pane_id)
            if found is not None:
                found[1].active_pane = pane
                with started_by(Origin.MOUSE):
                    note(self.pymux, Kind.CLICK, "select-pane")

        widget.on_mouse_focus = focus_pane_on_click
        self.register_pane(pane)
        return pane

    def _window_pane_shown(self) -> None:
        for client_state in self.pymux._client_states.values():
            client_state.sync_focus()
        self.pymux.invalidate(Woke.WINDOW_PANE_OPENED)

    def window_pane_drew(self, pane: Pane) -> None:
        "A window pane's window drew a frame: send it to every client that shows it."
        for client_state in self.pymux._client_states.values():
            client_state.refresh_window_pane(pane)

    def window_pane_ended(self, pane: Pane) -> None:
        "The window of a window pane closed. A window leaves nothing to remain on exit."
        self.take_ended_pane(pane)
        self.pymux.invalidate(Woke.PANE_ENDED)

    def close_window_panes(self) -> None:
        "Take every window pane away, as an upgrade does: no window survives its compositor."
        for pane in list(self.pymux.panes_by_id.values()):
            if is_window_pane(pane):
                pane.process.kill()
                self.take_ended_pane(pane)

    def register_pane(self, pane: Pane) -> None:
        "Record a pane, and tell it what the clients' terminals are."
        # Keep track of panes. This is a WeakKeyDictionary, we only add, but
        # don't remove.
        self.pymux.panes_by_id[pane.pane_id] = pane

        # A pane that starts now missed the last walk of the panes.
        self.pymux.terminals.tell_pane_about_keyboard(pane)
        self.pymux.terminals.tell_pane_about_colours(pane)
        self.pymux.terminals.tell_pane_about_cell_size(pane)

    def directory_to_start_in(self, window: Window | None, session: Session | None) -> str | None:
        """
        Where a new pane starts when `-c` named nothing.

        What a pane reported about itself wins over what the operating
        system says about it: the shell knows its logical directory on
        every system, and the process table only on some. What the OS
        says covers a pane that never reported, and nothing covers the
        first pane of a session. An explicit `-c` already won before
        this was asked.
        """
        pane = None
        if window is not None:
            pane = window.active_pane
        if pane is None and session is not None:
            active = session.arrangement.get_active_window()
            if active is not None:
                pane = active.active_pane
        if pane is not None and pane.current_directory:
            return pane.current_directory
        if window is not None and window.active_process:
            # When the path of the active process is known, start the
            # new process at the same location.
            return window.active_process.get_cwd()
        return None

    def kill_session(self, session: Session) -> None:
        """
        Kill every pane of a session, and take the session off.

        The last pane already takes the session, so the call below is
        for a session that had no pane to start with.
        """
        for window in list(session.arrangement.windows):
            for pane in list(window.panes):
                self.kill_pane(pane)

        if session in self.pymux.sessions:
            self.pymux.remove_session(session)

    def hang_up(self, pane: Pane) -> None:
        """
        End the program of a pane a person closed: SIGHUP, and SIGKILL
        after a grace. `hanging_up` keeps it, because a pane closed
        within the grace is in no snapshot, and the server that resumes
        one never sends that SIGKILL.
        """
        if isinstance(backend := pane.process.backend, PosixBackend):
            self.hanging_up.add(backend)
        pane.process.hang_up()

    async def create_window(
        self,
        command: str | None = None,
        start_directory: str | None = None,
        name: str | None = None,
        index: WindowIndex | None = None,
        session: Session | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
    ) -> Window:
        """
        Create a new :class:`pymux.core.window_tree.Window` in the arrangement,
        and answer it.

        `index` says where it goes. Without one it takes the lowest
        free index. Lillecarl/pymux#191.

        `session` says where the window goes. Without one it is the
        session of the client that asks. A window made for another
        session is not focused: its pane is in no layout the asking
        client draws.

        `task_group` is whose scope the pane's program runs in, and
        without one it is the server's. A window made outside
        `Pymux.running` names its own.
        """
        asked_for = self.pymux.current_session
        if session is None:
            session = asked_for

        pane = await self.create_pane(
            None, command, start_directory=start_directory, session=session, task_group=task_group
        )

        window = session.arrangement.create_window(pane, name=name, index=index)
        remember_the_size_made_at(window, pane)
        if session is asked_for:
            pane.focus()
        self.pymux.invalidate(Woke.WINDOW_OPENED)
        return window

    async def add_process(
        self,
        command: str | None = None,
        vsplit: bool = False,
        start_directory: str | None = None,
        window: Window | None = None,
        task_group: anyio.abc.TaskGroup | None = None,
        select: bool = True,
    ) -> Pane:
        """
        Add a new process to the given window (or the active window).
        (vsplit/hsplit). Answers the new pane. Without `select` the
        focus stays where it was: `split-window -d`.
        """
        if window is None:
            window = self.pymux.arrangement.get_active_window()
        if window is None:
            raise CommandException("no current window")

        pane = await self.create_pane(
            window,
            command,
            start_directory=start_directory,
            session=self.pymux.session_of_window(window),
            task_group=task_group,
        )
        window.add_pane(pane, vsplit=vsplit, select=select)
        if select:
            pane.focus()
        self.pymux.invalidate(Woke.PANE_WAS_SPLIT_OFF)
        return pane

    def kill_pane(self, pane: Pane) -> None:
        """
        Kill the given pane, and remove it from the arrangement.

        **The session it empties is not taken here.** The callback that
        runs when the process ends does that, and it has to: a pane
        ends on its own as often as a person kills one. Doing it in
        both places would end a session while the program in its last
        pane is still answering the signal.

        **SIGHUP, not SIGKILL**, the way closing a terminal ends what
        runs in it: a shell writes its history on the way out, and a
        program that ignores it gets SIGKILL after a grace.
        """
        # Remove from layout. The session the pane is in, and not the
        # session of whoever asked: `kill-pane -t` reaches across.
        session = self.pymux.session_holding(pane)
        if session is None:
            if not pane.process.is_terminated:
                self.hang_up(pane)
            return
        session.arrangement.remove_pane(pane)

        if pane.process.is_terminated:
            # Its end was heard already, under `remain-on-exit`, and
            # nothing calls back again.
            self.end_if_empty(session)
        else:
            self._killed_in[pane.pane_id] = session
            self.hang_up(pane)
