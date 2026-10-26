"""
The options of the server process, each declared once: one value for
every session. `pymux/option_fields.py` says what the declaration gives.
Lillecarl/pymux#757.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyte.environment import terminal_name
from pyte.keep import Keep

from . import introspect, log
from .enums import AudioServerMode, Clipboard, ExtendedKeys, ForwardMode, RenderMode
from .log import logger
from .option_fields import Options, option, options

if TYPE_CHECKING:
    from .main import Pymux

__all__ = ["ServerOptions"]


def _log_level(value: str) -> str:
    if value not in log.LEVELS:
        raise ValueError("Expecting one of: %s." % ", ".join(sorted(log.LEVELS)))
    return value


def _to_the_logger(pymux: Pymux) -> None:
    log.set_level(pymux.options.log_level)


def _say_the_level(pymux: Pymux, owner: object) -> None:
    logger.info("The log level is %s from now on.", pymux.options.log_level)


def _to_the_kernel(pymux: Pymux) -> None:
    # The kernel may refuse; the option then says what it took.
    wanted = pymux.options.allow_remote_debugging
    pymux.options.hold("allow_remote_debugging", introspect.let_debugger_attach(wanted))


def _wayland_changed(pymux: Pymux, owner: object) -> None:
    pymux.wayland.option_changed()


def _audio_changed(pymux: Pymux, owner: object) -> None:
    pymux.audio.option_changed()


def _keyboard_changed(pymux: Pymux, owner: object) -> None:
    # Every client hears what its terminal should send now.
    pymux.sync_keyboard()


def _render_mode_changed(pymux: Pymux, owner: object) -> None:
    pymux.apply_render_mode()


@options
class ServerOptions(Options):
    allow_remote_debugging: bool = option(
        False,
        doc=(
            "Whether another process of this user may attach a debugger to this server: "
            "`python -m pdb -p <pid>`, or py-spy. It lets anything they run read every pane's "
            "scrollback, so it stays off until asked. `pymux/introspect.py` answers most questions without it."
        ),
        on_write=_to_the_kernel,
        keep=Keep.REBUILT,
    )
    log_level: str = option(
        log.LogLevel.INFO.value,
        doc="How much this server writes to its log, changed while it runs. `--log-level` says it before it starts.",
        choices=sorted(log.LEVELS),
        parse=_log_level,
        on_set=_say_the_level,
        on_write=_to_the_logger,
        keep=Keep.REBUILT,
    )
    set_clipboard: Clipboard = option(
        Clipboard.EXTERNAL,
        doc=(
            "Who may write the clipboard of the person at the keyboard. "
            '"external" is tmux\'s default: a copy the person makes goes out, and a program in a '
            "pane is refused. Lillecarl/pymux#378."
        ),
    )
    wayland_server: bool = option(
        True,
        doc=(
            "Hold a Wayland compositor for the panes whose session has no display, so wl-copy and "
            "wl-paste work there. Needs the `wayland` extra; without it this does nothing. A pane "
            "that runs already keeps its environment. Lillecarl/pymux#383."
        ),
        on_set=_wayland_changed,
    )
    audio_server: AudioServerMode = option(
        AudioServerMode.AUTO,
        doc=(
            "Whose PipeWire holds the speaker and microphone that panes play into and record from. "
            "`AudioServerMode` says what each means. Only new panes see it. Lillecarl/pymux#657."
        ),
        on_set=_audio_changed,
    )
    forward_mode: ForwardMode = option(
        ForwardMode.ON,
        doc="What happens to a request to forward a port. `ForwardMode` says.",
    )
    job_ttl: int = option(
        3600,
        doc=(
            "How long a job outlives its last activity, in seconds. Output and completion both count. "
            "A running job is ended first, a finished one forgotten, and a waited one never. Zero keeps "
            "every job until the cap forgets the finished ones."
        ),
        choices=[0, 600, 3600, 86400],
    )
    action_log: bool = option(
        False,
        doc=(
            "Write what a person does to `actions.db`: each command, what started it, and the clicks "
            "that run none. Never what a pane shows or what is typed into one. `pymux/actions.py` says the rest."
        ),
    )
    action_log_limit: int = option(
        500_000,
        doc="The most rows `actions.db` keeps; the oldest go first. Zero keeps all.",
        choices=[0, 100_000, 500_000, 1_000_000],
    )
    default_terminal: str = option(
        terminal_name(),
        doc="The TERM a new pane starts with.",
        choices=["xterm", "xterm-256color", "screen"],
    )
    extended_keys: ExtendedKeys = option(
        ExtendedKeys.ON,
        doc=(
            'How much of the keyboard this server uses. "off" is the way out for a program that '
            "misbehaves under the extended encodings, and for a terminal that claims more than it "
            "does. Lillecarl/pymux#173."
        ),
        on_set=_keyboard_changed,
    )
    render_mode: RenderMode = option(
        RenderMode.OPTIMIZED,
        doc='"reference" draws every cell of every frame and keeps nothing: slow, and right. `RenderMode` says why.',
        on_set=_render_mode_changed,
    )
    test_mode: bool = option(
        False,
        doc=(
            "Show 13:37 on the 14th of March of this year wherever pymux shows a clock, so a screen "
            "does not change while nobody types and a picture cannot race the clock. Lillecarl/pymux#273."
        ),
    )
