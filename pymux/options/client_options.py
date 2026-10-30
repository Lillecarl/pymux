"""
The options of one attached terminal, each declared once.
`set-client-option` writes them, and a client announces the ones its own
configuration file names when it attaches. `pymux/options/option_fields.py`
says what the declaration gives.

**Only a client can know these:** what colours its terminal draws with,
and which way round they are. An answer kept on the server is one answer
for two people who may sit at very different screens. Lillecarl/pymux#223.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..enums import AudioFrame, ClientAudio, FileTransfer, NotificationPopups, WindowScroll, Woke
from ..style.nearest import NEAREST
from ..style.style import THEMES
from .option_fields import Options, option, options

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..core.main import Pymux

__all__ = ["AUDIO_BITRATES", "ClientOptions", "theme_names"]

#: What Opus encodes at, in kbit/s.
AUDIO_BITRATES = range(6, 511)


def theme_names() -> list[str]:
    """
    Every name `theme` takes, `nearest` first because it is the default:
    a person types it to go back to the search after trying a theme. The
    search reads this list too, and skips `nearest`.
    """
    from ..style.style_base16 import names as base16_names
    from ..style.style_pygments import names

    return (
        [NEAREST]
        + sorted(THEMES)
        + ["pygments:%s" % (name,) for name in names()]
        + ["base16:%s" % (name,) for name in base16_names()]
    )


def _parse_theme(value: str) -> str:
    # `nearest` is not a theme: it is the name for letting the terminal decide.
    if value == NEAREST:
        return value
    source, _, rest = value.partition(":")
    if source == "pygments":
        from ..style.style_pygments import names

        if rest not in names():
            raise ValueError("Expecting the name of a pygments style, like: %s." % (", ".join(names()[:6]),))
    elif source == "base16":
        from ..style.style_base16 import names

        if rest not in names():
            raise ValueError("Expecting the name of a base16 scheme, like: %s." % (", ".join(names()[:6]),))
    elif value not in THEMES:
        raise ValueError("Expecting one of: %s." % ", ".join(sorted(THEMES)))
    return value


def _theme_chosen(pymux: Pymux, client: ClientState) -> None:
    # The colour base of every pane follows which theme owns the
    # screen; `paint-screen` says whether one does. Lillecarl/pymux#283.
    pymux.terminals.sync_color_bases()
    pymux.invalidate(Woke.THEME_WAS_CHOSEN)


def _swapped(pymux: Pymux, client: ClientState) -> None:
    pymux.terminals.sync_color_scheme()


def _theme_shown(value: str, client: ClientState | None) -> str:
    # `nearest` alone says nothing about what is on the screen.
    matched = getattr(client, "matched", None)
    if value != NEAREST or matched is None:
        return value
    return "%s (%s)" % (NEAREST, matched)


def _parse_bitrate(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number not in AUDIO_BITRATES:
        raise ValueError("Expecting kbit/s from %d to %d." % (AUDIO_BITRATES[0], AUDIO_BITRATES[-1]))
    return number


def _audio_changed(pymux: Pymux, client: ClientState) -> None:
    pymux.audio.client_options_changed()


@options
class ClientOptions(Options):
    name: str = option(
        "",
        doc=(
            'What a person calls this terminal: "desk", "phone". `detach-client -t` and `set-client-option -t` '
            "take it, and it is drawn over the name pymux derives. It lasts as long as the attachment. "
            "Lillecarl/pymux#340."
        ),
    )
    theme: str = option(
        NEAREST,
        doc=(
            "The colour scheme this client draws with: one of pymux's, `pygments:<name>` or `base16:<name>`. "
            "`nearest` asks the terminal what colours it draws with and takes the known theme closest to the "
            "answer. Lillecarl/pymux#194, Lillecarl/pymux#282, Lillecarl/pymux#346."
        ),
        choices=theme_names,
        parse=_parse_theme,
        on_set=_theme_chosen,
        show=_theme_shown,
    )
    swap_light_and_dark_colors: bool = option(
        False,
        doc="The theme is the other way round from this terminal: draw it swapped.",
        on_set=_swapped,
    )
    full_screen: bool = option(
        False,
        doc=(
            "One pane over every cell of this terminal, with nothing that pymux draws for itself. It hides "
            "`status` and `pane-border-status` without changing them. Lillecarl/pymux#471."
        ),
    )
    window_scroll: WindowScroll = option(
        WindowScroll.SMOOTH,
        doc=(
            'What a scroll does to a Wayland window: "smooth" for a trackpad, "notch" for a mouse wheel. '
            "The terminal does not say which it has. Lillecarl/pymux#652."
        ),
    )
    browser_program: str = option(
        "",
        doc="The browser a program on the server drives on this machine: empty for the first Chromium found. "
        "Lillecarl/pymux#653.",
    )
    browser_profile: str = option(
        "", doc="The profile that browser runs with: empty for one pymux keeps. Lillecarl/pymux#653."
    )
    frames_in_flight: int = option(
        5,
        doc=(
            "The most frames on their way to this terminal at once, over a link where its answer says what "
            "arrived. More lets an animation go faster than one frame a round trip; a key's frame waits "
            "behind the ones ahead of it. 0 sets no limit. Lillecarl/pymux#259, Lillecarl/pymux#825."
        ),
        choices=[1, 2, 5, 10],
    )
    bytes_in_flight: int = option(
        4096,
        doc=(
            "The most bytes of frames on their way to this terminal at once. A frame always goes when none "
            "is, so a slow link still gets the newest screen and never a backlog. 0 sets no limit. "
            "Lillecarl/pymux#825."
        ),
        choices=[1024, 4096, 16384, 65536],
    )
    audio: ClientAudio = option(
        ClientAudio.ON,
        doc="Whether this machine plays the panes' sound and lends its microphone. Lillecarl/pymux#657.",
        on_set=_audio_changed,
    )
    audio_rtt_multiplier: float = option(
        2.0,
        doc="How many round trips of sound this client buffers: two cover a round trip that doubles.",
        on_set=_audio_changed,
    )
    audio_frame: AudioFrame = option(
        AudioFrame.AUTO, doc="How much sound a packet carries, in ms: any size Opus encodes.", on_set=_audio_changed
    )
    audio_speaker_bitrate: int = option(
        128,
        doc="What the sound costs the link towards this client, in kbit/s. Lillecarl/pymux#674.",
        choices=[64, 128, 256],
        parse=_parse_bitrate,
        on_set=_audio_changed,
    )
    audio_microphone_bitrate: int = option(
        64,
        doc="What the microphone costs the link from this client, in kbit/s. Lillecarl/pymux#674.",
        choices=[24, 64, 128],
        parse=_parse_bitrate,
        on_set=_audio_changed,
    )
    file_transfer: FileTransfer = option(
        FileTransfer.AUTO,
        doc=(
            "Who saves a file a pane sends to the person: this terminal, or the pymux client on the "
            "person's machine. Lillecarl/pymux#743."
        ),
    )
    notification_popups: NotificationPopups = option(
        NotificationPopups.AUTO,
        doc=(
            "Whether a notification shows as a box inside pymux: always, never, or with auto only when "
            "this terminal does not show desktop notifications itself. Lillecarl/pymux#826."
        ),
    )
    notification_popup_seconds: float = option(
        5.0,
        doc="How long a notification box stays on screen. Lillecarl/pymux#826.",
    )
