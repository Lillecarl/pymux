"""
What a pane says past its own screen, and where it goes.

A program writes OSC sequences that only the terminal of a person can
serve: the clipboard, a notification, a URL to open, a file to save.
Some are read here and kept on the pane, some are spelled for each
client's terminal, and none goes out unchecked.
"""

from __future__ import annotations

import base64
import shlex
from typing import TYPE_CHECKING, Any

from libpymux.protocol import Field, Message, Packet
from prompt_toolkit.clipboard import ClipboardData
from pyte.osc import CLEAR_SCROLLBACK, Osc
from pyte.page import RowMark

from .downloads import (
    FILE_KEYS,
    DownloadRefused,
    FinishedDownload,
    file_packets,
    route_of,
    sequences_of,
)
from .enums import (
    Clipboard,
    FileTransfer,
    ForwardMode,
    OpenUrlMode,
    Woke,
)
from .forwarding import loopback_port
from .inline_images import as_png, cells_of
from .log import logger
from .notifications import (
    NotificationSource,
    Urgency,
    is_question,
)
from .osc import (
    build_osc,
    copy_data_of,
    current_dir_of,
    extension_key_of,
    file_url_dir_of,
    ftcs_of,
    message_of,
    open_url_of,
    progress_of,
    remote_host_of,
    request_attention_of,
    set_user_var_of,
    shell_integration_of,
    simple_notification_of,
)
from .window_pane import WindowScreen

if TYPE_CHECKING:
    from .arrangement import Pane
    from .client_state import ClientState
    from .main import Pymux

__all__ = ["MAX_USER_VARS", "Outward"]

#: How many user variables one pane keeps, from "OSC 1337 ;
#: SetUserVar". A shell sets the same handful on every prompt; a
#: program that mints names without end loses the oldest, the way the
#: notification hub does.
MAX_USER_VARS = 64

#: The rows "OSC 133" marks: where a prompt starts, and where the output
#: of a command starts.
_FTCS_ROW_MARKS = {"A": RowMark.PROMPT, "C": RowMark.OUTPUT}


def _open_question(url: str, forward: dict[str, Any] | None) -> str:
    """
    What a person is asked before a URL opens on their machine.

    **The forward is named, because it is part of what a yes allows.**
    Opening a page is one thing; binding a port on the machine somebody
    is sitting at is another, and a question that hid the second would
    be collecting an answer to a smaller question than it asked.
    Lillecarl/pymux#437.
    """
    if forward is None:
        return "Open %s in the browser? (y/n)" % (url,)

    return "Open %s in the browser, and forward %s:%d here? (y/n)" % (
        url,
        forward["host"],
        forward["port"],
    )


class Outward:
    "What every pane sends past its screen, read and passed on."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

    def open_url(self, url: str, confirmed: bool = False) -> None:
        """
        Open a URL in the browser of a client, or ask first.

        `open-url-mode` decides: "open" sends the packet, "ask" shows
        the question in the command bar of every client it would land
        on -- a yes runs "open-url -c", so the question is asked once
        -- and "off" drops it.

        The packet goes to the client and not to the server, because
        the browser of the user runs on the machine of the client. The
        client picks the way its platform opens one: "open" on macOS,
        "xdg-open" or what $BROWSER names on Linux.

        **A loopback URL travels with its port.** The packet then
        carries what to forward, and the client binds it before it
        opens anything. `url_forward` decides, and the question below
        says so, because binding a port here is part of what a yes
        allows. Lillecarl/pymux#437.
        """
        options = self.pymux.current_session.options
        if options.open_url_mode == OpenUrlMode.OFF:
            logger.info("Not opening %s: open-url-mode is off.", url)
            return

        clients = self.pymux.clients_to_open_on()
        if not clients:
            self.pymux.add_command_error("Nobody is attached to open %s." % (url,))
            return

        forwards = [self.url_forward(url, client_state) for client_state in clients]

        # **A `-c` counts only from a person.** It is the answer to a
        # question, and a pane that sends the flag itself answered
        # nothing -- without this, `open-url-mode ask` reads as a gate
        # and is not one. Lillecarl/pymux#443.
        confirmed = confirmed and self.pymux.a_person_asked()

        asking = options.open_url_mode == OpenUrlMode.ASK or (
            self.pymux.options.forward_mode == ForwardMode.ASK and any(forwards)
        )

        if asking and not confirmed:
            command = "open-url -c %s" % (shlex.quote(url),)
            for client_state, forward in zip(clients, forwards):
                client_state.ask(_open_question(url, forward), command)
            return

        for client_state, forward in zip(clients, forwards):
            packet: Message = {Field.CMD: Packet.OPEN, Field.DATA: url}
            if forward is None:
                client_state.message = "Opened %s in the browser of this machine." % (url,)
            else:
                # The client says what it opened, once it knows: a port
                # that was taken here moves the URL, and a message from
                # this side would name the address that did not work.
                packet[Field.FORWARD] = forward
            client_state.connection._send_packet(packet)

    def url_forward(self, url: str, client_state: ClientState) -> dict[str, Any] | None:
        """
        What to forward so that this URL works on that client, or
        `None`.

        **`None` for nearly every URL**, and for every client that is
        not on another machine. A forward is only the answer when the
        address means "the machine the server runs on" and the browser
        is somewhere else. Lillecarl/pymux#437.
        """
        if not client_state.connection.can_forward:
            return None
        options = self.pymux.current_session.options
        if not options.open_url_forward:
            return None
        if self.pymux.options.forward_mode == ForwardMode.OFF:
            # Somebody turned port forwarding off. A URL is not a way
            # around that.
            return None

        found = loopback_port(url)
        if found is None:
            return None

        host, port = found
        return {"host": host, "port": port, "idle": options.open_url_forward_idle}

    def forward_osc(self, pane: Pane, code: str, param: str) -> None:
        """
        Write an OSC sequence of a pane to the terminals of the
        clients. ptterm hands over the three that only the terminal of
        the user can serve: the clipboard, a desktop notification and
        the shape of the pointer.

        The payload comes from a program in a pane, so `build_osc`
        checks it before it reaches the terminal of the user.

        Never raises: this runs on the read path of a pane, and one
        sequence may not stop it.
        """
        try:
            # A notification the hub recorded goes to a terminal that
            # reports clicks as OSC 99 instead (`_notified`).
            recorded = False

            if code == Osc.POINTER_SHAPE:
                # The shape of the pointer is not a sequence to pass on
                # as it arrives: it belongs to the pane, and the client
                # has to see the shape of the pane it looks at. The
                # screen of the pane holds it; this only asks every
                # client to look again.
                self.pymux.terminals.sync_pointer_shape()
                return

            if code == Osc.TERMINAL_EXTENSION:
                # iTerm2's namespace, one subcommand at a time. A
                # subcommand this session parsed may reach the terminal
                # of the user; one it did not parse must not, because
                # an unchecked payload drives that terminal instead of
                # only naming a service.
                if not self._read_terminal_extension(pane, param):
                    return
                recorded = request_attention_of(param) not in (None, "no")

            if code == Osc.CURRENT_DIRECTORY:
                # Where the program is, as a file URL. The outer
                # terminal keeps its own copy, so this travels on; the
                # pane keeps one too.
                self._read_current_directory(pane, param)

            if code == Osc.SHELL_INTEGRATION:
                # The prompt, the command and the exit status it
                # finished with, which the pane keeps. **They stay
                # inside**, as tmux keeps them: they name rows of one
                # pane, not of the client's screen, and iTerm2 reads a
                # prompt start as the end of the alternate screen
                # (`softAlternateScreenMode = NO`), so every frame after
                # one drew on its main screen, a row off.
                self._read_shell_integration(pane, param)
                return

            if code == Osc.CLIPBOARD:
                # **Only "on" lets a program in a pane near the
                # clipboard.** tmux asks the same question in
                # `input_osc_52`, and its default answer is no. A copy
                # that the person makes asks `write_user_clipboard`
                # instead, which is the other half of the option.
                if self.pymux.options.set_clipboard is not Clipboard.ON:
                    return
                # What a pane copies goes into the paste buffer of the
                # session as well, so that "paste-buffer" can put it in
                # another pane. This stays one way: a pane may write the
                # clipboard, and may not read what the user copied
                # somewhere else.
                self._mirror_clipboard(param)

            if code == Osc.NOTIFICATION:
                # Give the notification an identifier that names this
                # pane, so that the answer finds its way back.
                param = self.pymux.notifications.outgoing(pane.pane_id, param)
                # And keep it for the hub, which lists what the
                # terminal was asked, not only what it showed.
                self.pymux.notification_center.add_osc99(pane.pane_id, param)

            if code == Osc.SIMPLE_NOTIFICATION:
                # No identifier and no answer, so nothing to route.
                notice = simple_notification_of(param)
                if notice is None:
                    return
                title, body = notice
                self.pymux.notification_center.add(title, body, pane_id=pane.pane_id, source=NotificationSource.OSC_777)
                recorded = True

            if code == Osc.MESSAGE:
                text = message_of(param)
                # ConEmu's numbered commands drive ConEmu itself, but for
                # the progress, which each client's terminal is given
                # for the pane it looks at.
                if text is None:
                    progress = progress_of(param)
                    if progress is not None and progress != pane.progress:
                        pane.progress = progress
                        if progress.state:
                            self.pymux.progressing.add(pane.pane_id)
                        else:
                            self.pymux.progressing.discard(pane.pane_id)
                        self.pymux.terminals.sync_progress()
                        self.pymux.invalidate(Woke.PANE_PROGRESSED)
                    return
                self.pymux.notification_center.add(text, pane_id=pane.pane_id, source=NotificationSource.OSC_9)
                recorded = True

            # A person looking at the window already sees what the
            # notification says: it stays in the hub, seen. A terminal
            # without the focus is no look. Lillecarl/pymux#799.
            notice = recorded or (code == Osc.NOTIFICATION and not is_question(param))
            if notice and pane.pane_id in self.pymux._panes_in_sight():
                logger.debug("Kept a notification of pane %%%d: a terminal with the focus shows it.", pane.pane_id)
                return

            sequence = build_osc(code, param)
            if sequence is None:
                logger.warning("Dropped an unsafe OSC %s of a pane.", code)
                return

            # The clipboard and a notification come from any pane: a
            # build that ends in a pane out of sight is exactly what a
            # notification is for.
            #
            # A notification reaches each terminal in the OSC that
            # terminal shows, and `_emit` spells it once it is
            # finished. Only OSC 99 to a terminal that shows OSC 99 goes
            # as it came: chunks, buttons, closes and all.
            # Lillecarl/pymux#497.
            for connection in self.pymux._client_states:
                # No connection is a socket command's temporary
                # client: nothing to send to. Lillecarl/pymux#493.
                if connection is None or code in (Osc.SIMPLE_NOTIFICATION, Osc.MESSAGE):
                    continue
                if code == Osc.NOTIFICATION and connection.notification_osc is not Osc.NOTIFICATION:
                    continue
                connection.forward_osc(sequence)

            # **The escape travels with the next frame, so there has to
            # be one.** A pane that writes wakes the clients watching
            # it, and those would draw anyway; a pane in a window
            # nobody looks at draws no frame at all, and a notification
            # from one is what the line above is for. Without this, its
            # escape would sit in a client's buffer until something
            # else happened to draw. Lillecarl/pymux#478.
            self.pymux.invalidate(Woke.PANE_WROTE_AN_OSC)
        except Exception:
            logger.exception("Forwarding an OSC sequence failed.")

    def _read_current_directory(self, pane: Pane, param: str) -> None:
        """
        Keep where the program says it is.

        "OSC 7" carries a file URL with a host and a path. A URL that
        does not parse stores nothing; the sequence still travels on,
        because the outer terminal reads it for itself.
        """
        parsed = file_url_dir_of(param)
        if parsed is None:
            return
        host, directory = parsed
        pane.current_host = host
        pane.current_directory = directory

    def _read_shell_integration(self, pane: Pane, param: str) -> None:
        """
        Keep the shell integration zone and the exit status.

        "OSC 133" names the prompt (A), the command (B), its output
        (C) and its end (D, with the status or bare for an abort). An
        abort clears the zone and the status; anything else that does
        not parse stores nothing. The sequence travels on either way,
        for the zones of the outer terminal.
        """
        parsed = ftcs_of(param)
        if parsed is None:
            return
        letter, status = parsed
        if letter == "D":
            pane.command_zone = None
            pane.last_exit_status = status
        else:
            pane.command_zone = letter
        # The rows copy mode jumps between, as tmux's `input_osc_133`
        # marks them. Lillecarl/pymux#500.
        mark = _FTCS_ROW_MARKS.get(letter)
        screen = pane.screen
        if mark is not None and not isinstance(screen, WindowScreen):
            screen.mark_row(mark)

    def _read_terminal_extension(self, pane: Pane, param: str) -> bool:
        """
        Read one subcommand of "OSC 1337". True when the sequence may
        travel on to the terminal of the user.

        The browser keeps its question (`open-url-mode` asks it). The
        working directory, a user variable, a mark, an attention ask
        and a clipboard write travel on once parsed, the way the
        sequences they synonymise do. A download goes to one client,
        through `send_download`. Anything else stays inside: images
        wait for their slice, and an unchecked payload never reaches
        the terminal of the user.
        """
        url = open_url_of(param)
        if url is not None:
            self.open_url(url)
            return False

        keyed = extension_key_of(param)
        if keyed is None:
            return False
        key, rest = keyed

        if key == "CurrentDir":
            directory = current_dir_of(rest)
            if directory is None:
                return False
            pane.current_directory = directory
            return True

        if key == "StealFocus":
            if rest or not self.pymux.options_of_pane(pane).steal_focus:
                return False
            session = self.pymux.session_holding(pane)
            for client_state in list(self.pymux._client_states.values()):
                if client_state.session is session and not client_state.temporary:
                    self.pymux.show_pane(client_state, pane)
            # And the terminal of the user raises its own window.
            return True

        if key == "RemoteHost":
            remote = remote_host_of(rest)
            if remote is None:
                return False
            pane.current_user, pane.current_host = remote
            return True

        if key == "ShellIntegrationVersion":
            integration = shell_integration_of(rest)
            if integration is None:
                return False
            pane.shell_integration_version, pane.shell = integration
            return True

        if key == "SetMark":
            if rest:
                return False
            # iTerm2's mark is where a person wants to come back to,
            # which is what a prompt mark is for.
            screen = pane.screen
            if not isinstance(screen, WindowScreen):
                screen.mark_row(RowMark.PROMPT)
            return True

        if key == "SetUserVar":
            parsed = set_user_var_of(rest)
            if parsed is None:
                return False
            name, value = parsed
            if name not in pane.user_vars and len(pane.user_vars) >= MAX_USER_VARS:
                del pane.user_vars[next(iter(pane.user_vars))]
            pane.user_vars[name] = value
            return True

        if key == "RequestAttention":
            value = request_attention_of(param)
            if value is None:
                return False
            # A withdrawal cancels a request the hub never tracked as
            # one: there is nothing to take back, so only the asks
            # record anything.
            if value != "no":
                self.pymux.notification_center.add(
                    "Attention requested",
                    "With fireworks." if value == "fireworks" else "",
                    Urgency.CRITICAL if value == "yes" else Urgency.NORMAL,
                    pane.pane_id,
                    NotificationSource.ATTENTION,
                )
            return True

        if key == "Copy":
            data = copy_data_of(param)
            if data is None:
                return False
            # The same gate as OSC 52: only "on" lets a program in a
            # pane near the clipboard.
            if self.pymux.options.set_clipboard is not Clipboard.ON:
                return False
            self._mirror_clipboard("c;" + data)
            return True

        if key == CLEAR_SCROLLBACK:
            # The pane's screen has cleared its own history already.
            return not rest

        if key in FILE_KEYS:
            # A download goes to one client, which `send_download`
            # chooses; it is never this broadcast.
            self._read_download(pane, key, rest)
            return False

        return False

    def _read_download(self, pane: Pane, key: str, rest: str) -> None:
        "Read one sequence of a pane's download, and send a whole file on."
        try:
            finished = pane.downloads.read(key, rest)
        except DownloadRefused as refused:
            client = self.download_client(pane)
            logger.warning("Refused a download of pane %s: %s", pane.pane_id, refused)
            if client is not None:
                client.message = str(refused)
            return
        if finished is not None and finished.inline:
            self._draw_inline_image(pane, finished)
        elif finished is not None:
            self.send_download(pane, finished)

    def _draw_inline_image(self, pane: Pane, finished: FinishedDownload) -> None:
        """
        Place an iTerm2 inline image at the pane's cursor, as pyte places
        a kitty image. Something that is no image draws nothing. Lillecarl/pymux#789.
        """
        screen = pane.screen
        if isinstance(screen, WindowScreen):
            return
        found = as_png(finished.data)
        if found is None:
            logger.warning("Dropped an inline image of pane %s that does not decode.", pane.pane_id)
            return
        png, pixels = found
        columns, rows = cells_of(
            finished.arguments, pixels, (screen.cell_width, screen.cell_height), (screen.columns, screen.lines)
        )
        screen.graphics.add_png(
            png, columns, rows, screen, move_cursor=finished.arguments.get("doNotMoveCursor") != "1"
        )

    def download_client(self, pane: Pane) -> ClientState | None:
        """
        The client a download from this pane goes to: of the clients on
        the pane's session, the one a person used last. `None` when
        nobody is attached to that session.

        **One client and not every one.** A file written twice is not
        what anybody asked for, and the clipboard's broadcast is not a
        rule for it. A person in another session did not ask for it
        either.
        """
        session = self.pymux.session_holding(pane)
        if session is None:
            return None
        clients = self.pymux.clients_on(session)
        if not clients:
            return None
        return max(clients, key=lambda client: client.last_used)

    def send_download(self, pane: Pane, download: FinishedDownload) -> None:
        """
        Give a whole file from a pane to the person, the way the
        client's `file-transfer` says. Lillecarl/pymux#743.
        """
        client = self.download_client(pane)
        if client is None:
            logger.warning("Dropped the download %s of pane %s: nobody is attached.", download.name, pane.pane_id)
            return
        # Only `SshClient` says it can forward, so this is the fact that
        # the client came over SSH.
        terminal_saves = client.connection.identity.saves_downloads
        route = route_of(client.options.file_transfer, client.connection.can_forward, terminal_saves)
        refused = None
        if route is FileTransfer.PASSTHROUGH and not terminal_saves:
            refused = "file-transfer is passthrough, and this terminal saves no iTerm2 download"
        elif route is FileTransfer.PYMUX and not client.connection.saves_files:
            refused = "this pymux client is too old to save one; start a new one"
        if refused is not None:
            message = "Did not send %s: %s." % (download.name, refused)
            logger.warning(message)
            client.message = message
            self.pymux.invalidate(Woke.PANE_WROTE_AN_OSC)
            return
        if route is FileTransfer.PASSTHROUGH:
            for sequence in sequences_of(download):
                client.connection.forward_osc(sequence)
            # The sequences go behind a frame, so there has to be one.
            self.pymux.invalidate(Woke.PANE_WROTE_AN_OSC)
            return
        for packet in file_packets(download):
            client.connection._send_packet(packet)

    def copied_in_copy_mode(self, text: str) -> None:
        """
        Copy mode is a client's, so what it copied goes to that client's
        terminal and to nobody else's. Another person on the session
        keeps their own clipboard. Lillecarl/pymux#470.
        """
        try:
            client = self.pymux.get_client_state()
        except ValueError:
            client = None
        self.write_user_clipboard(text, client=client)

    def write_user_clipboard(self, text: str, selection: str = "c", client: ClientState | None = None) -> None:
        """
        Put what a person copied on the clipboard of their terminal, or
        on the selection OSC 52's letter names: "p" is the primary. With
        `client`, on that client's terminal alone.

        Copy mode calls this, and so does a pane's wl-copy, which
        `wayland.py` lets through only under "on". **It is not `forward_osc`**, although the
        sequence is the same one: the two have different answers under
        `set-clipboard`, because a person asking for their own
        clipboard is not a program in a pane asking for it. tmux keeps
        them apart the same way -- a copy asks only that the option is
        not "off". Lillecarl/pymux#378.

        The paste buffer of the session is not written here. Copy mode
        writes it through the clipboard of the application, which is
        that buffer, and the selection type goes with it.

        Never raises: this runs under a key press.
        """
        if self.pymux.options.set_clipboard is Clipboard.OFF or not text:
            return
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
            sequence = build_osc(Osc.CLIPBOARD, selection + ";" + payload)
            if sequence is None:
                # The payload is base64 and the length is the only
                # thing that can fail it. A person who copies half a
                # megabyte gets nothing, and has to know why.
                logger.warning("The copy is too long for the clipboard.")
                return
            connections = list(self.pymux._client_states) if client is None else [client.connection]
            for connection in connections:
                # No connection is a socket command's temporary
                # client: nothing to send to. Lillecarl/pymux#493.
                if connection is None:
                    continue
                connection.forward_osc(sequence)
            # The copy travels with the next frame, so there has to be
            # one. Copy mode's key press draws anyway; a pane's wl-copy
            # draws nothing, and its copy would wait for some other
            # frame.
            self.pymux.invalidate(Woke.CLIPBOARD_WRITTEN)
        except Exception:
            logger.exception("Writing the clipboard of the user failed.")

    def _mirror_clipboard(self, param: str) -> None:
        """
        Put what a pane copied into the paste buffer of the session.

        The payload is "selections ; base64". An empty payload clears a
        selection of the user, which is not a reason to throw away the
        buffer of the session.
        """
        _selections, _semicolon, data = param.partition(";")
        if not data:
            return
        try:
            text = base64.b64decode(data.encode("ascii")).decode("utf-8", "replace")
        except Exception:
            return  # Not base64. `build_osc` drops it as well.
        self.pymux.clipboard.set_data(ClipboardData(text))
