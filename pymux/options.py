"""
All configurable options which can be changed through "set-option" commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, override

from .arrangement import Window
from .enums import (
    AudioFrame,
    ClientAudio,
    FileTransfer,
    WindowScroll,
    Woke,
)
from .key_mappings import PYMUX_TO_PROMPT_TOOLKIT_KEYS
from .key_spelling import key_however_it_is_written
from .nearest import NEAREST
from .option_fields import Declared, Options
from .server_options import ServerOptions
from .session import OwnOptions
from .session_options import SessionOptions
from .style import THEMES
from .window_options import WindowOptions

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux

    #: What an option is written on and read from: a client, one
    #: session's own values, or the server's, which hold the defaults.
    type Holder = ClientState | OwnOptions | Pymux

__all__ = [
    "ALL_CLIENT_OPTIONS",
    "ALL_OPTIONS",
    "ALL_SERVER_OPTIONS",
    "ALL_WINDOW_OPTIONS",
    "OnOffOption",
    "Option",
    "Scope",
    "SetOptionError",
]


class Scope(StrEnum):
    """
    What an option belongs to.

    A server option belongs to the process: its log, its compositor,
    what it tells the terminal it is. tmux's `options-table.c` marks
    the same names `OPTIONS_TABLE_SERVER`, and finds the table by the
    name, so `set-option` takes one with or without `-s`.
    Lillecarl/pymux#757.

    A session option belongs to the session. A window option belongs
    to one window, and `-g` says what a new one starts with.

    **A client option belongs to one attached terminal.** Only a client
    can know what its terminal is -- what colours it draws with, which
    way round they are -- so an answer kept on the server is one answer
    for two people who may be sitting at very different screens.
    Lillecarl/pymux#223.
    """

    SERVER = "server"
    SESSION = "session"
    WINDOW = "window"
    CLIENT = "client"


class Option(ABC):
    """
    Base class for all options.
    """

    #: What this option belongs to. `show-options`,
    #: `show-window-options` and `show-client-options` split the tables
    #: on this. Lillecarl/pymux#298, Lillecarl/pymux#223.
    scope: Scope = Scope.SESSION

    #: Where the value lives, on the object the option belongs to.
    #: An option that holds its state somewhere else -- the prefix
    #: key lives in the binding manager -- says None, and reads as
    #: not set. Lillecarl/pymux#298.
    attribute_name: str | None = None

    def held_by(self, pymux: Pymux, target: Holder | None = None) -> Holder | Window | None:
        """
        The object this option is written on and read from.

        `target` is the one the command chose: `set-client-option -t`
        names a client, and nothing else names anything yet. Without
        one, a window option takes the active window and a client
        option takes `the_client_to_tell()` -- the rule pymux already
        has for "which client does a command mean", because a command
        from a pane's CLI runs under a temporary client that is nobody.
        Lillecarl/pymux#272.
        """
        if target is not None:
            return target

        if self.scope is Scope.WINDOW:
            # A configuration file is read before the first window is
            # made, so `set-window-option` in one asked the arrangement
            # for a window it did not have and got an `IndexError` out
            # of `windows[0]`. That is not a `SetOptionError`, so it
            # left `source-file` and took the startup with it: pymux
            # drew nothing at all. Lillecarl/pymux#199.
            if not pymux.arrangement.windows:
                raise SetOptionError(
                    "There is no window yet. A window option belongs to one "
                    "window, so a configuration file has none to set. "
                    'Use "-g" to say what every new window starts with.'
                )
            return pymux.arrangement.get_active_window()

        if self.scope is Scope.CLIENT:
            client = pymux.the_client_to_tell()
            if client is None:
                raise SetOptionError(
                    "There is no client. A client option belongs to one "
                    "attached terminal, and a client announces its own "
                    "when it attaches."
                )
            return client

        return pymux

    def _attribute(self) -> str:
        """
        The attribute this option is written on.

        A `None` says the option keeps its value somewhere else, the
        way the prefix key lives in the binding manager. No option that
        reaches a `setattr` is one of those.
        """
        if self.attribute_name is None:
            raise SetOptionError("This option holds its value somewhere else.")
        return self.attribute_name

    def read(self, holder: Holder | Window | None) -> object:
        "What `holder` holds for this option, or None."
        return None if self.attribute_name is None else getattr(holder, self.attribute_name, None)

    def as_written(self, value: object, holder: Holder | Window | None) -> str:
        """
        What `show-options` prints for a value this option holds.

        The on/off options hold booleans and a person writes on and
        off; everything else prints as it is. An option whose value
        does not say the whole story says the rest here -- `theme
        nearest` names the theme it matched. Lillecarl/pymux#346.
        """
        if isinstance(value, bool):
            return "on" if value else "off"
        return str(value)

    @abstractmethod
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        """
        Return the possible values, as strings. (For autocompletion.)
        """

    @abstractmethod
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        "Set option. This can raise SetOptionError."

    def set_default(self, pymux: Pymux, value: str) -> None:
        """
        Say what a new window starts with, without changing one.

        `set-window-option -g` reaches this, and only a window option
        has anything to say: a session option is already one value for
        the whole session. Lillecarl/pymux#199.
        """
        raise SetOptionError("This option belongs to the session, so it is already global.")


class SetOptionError(Exception):
    """
    Raised when setting an option fails.
    """

    def __init__(self, message: str) -> None:
        self.message = message


class OnOffOption(Option):
    """
    Boolean on/off option.
    """

    def __init__(self, attribute_name: str, scope: Scope = Scope.SESSION) -> None:
        self.attribute_name = attribute_name
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return ["on", "off"]

    def _read(self, value: str) -> bool:
        "The value as a boolean, or a `SetOptionError`."
        value = value.lower()
        if value not in ("on", "off"):
            raise SetOptionError('Expecting "yes" or "no".')
        return value == "on"

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        setattr(self.held_by(pymux, target), self._attribute(), self._read(value))


class FieldOption(Option):
    """
    An option declared as a field of its scope's options, which say
    everything this does. Lillecarl/pymux#769.
    """

    def __init__(self, declared: Declared, scope: Scope, held_as: str = "options") -> None:
        self.declared = declared
        self.scope = scope
        self.attribute_name = declared.attribute
        #: The attribute of the owner that holds its options: a window's
        #: `options`, and the server's `session_defaults` for a session option.
        self.held_as = held_as

    def _options_of(self, owner: Holder | Window | None) -> Options | OwnOptions:
        "The options `owner` holds, which a field option is written on."
        if isinstance(owner, OwnOptions):
            return owner
        options = getattr(owner, self.held_as, None)
        if not isinstance(options, Options):
            raise SetOptionError("This is a %s option." % self.scope)
        return options

    @override
    def read(self, holder: Holder | Window | None) -> object:
        return getattr(self._options_of(holder), self.declared.attribute)

    def _parse(self, value: str) -> object:
        try:
            return self.declared.parse(value)
        except ValueError as error:
            raise SetOptionError(str(error)) from None

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        try:
            now = self.read(self.held_by(pymux))
        except SetOptionError:
            now = None
        return self.declared.values(now)

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        parsed = self._parse(value)
        owner = self.held_by(pymux, target)
        setattr(self._options_of(owner), self.declared.attribute, parsed)
        if self.declared.on_set is not None:
            self.declared.on_set(pymux, owner)

    @override
    def set_default(self, pymux: Pymux, value: str) -> None:
        "What every new window starts with. Changes no window that is open."
        if self.scope is not Scope.WINDOW:
            return super().set_default(pymux, value)
        pymux.arrangement.window_defaults[self.declared.attribute] = self._parse(value)


class StringOption(Option):
    """
    String option, written on whatever the scope says holds it.
    """

    def __init__(
        self, attribute_name: str, possible_values: Iterable[str] | None = None, scope: Scope = Scope.SESSION
    ) -> None:
        self.attribute_name = attribute_name
        self.possible_values = possible_values or []
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        try:
            now: str = getattr(self.held_by(pymux), self._attribute())
        except SetOptionError:
            return sorted(set(self.possible_values))
        return sorted(set(self.possible_values) | {now})

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        setattr(self.held_by(pymux, target), self._attribute(), value)


class PositiveIntOption(Option):
    """
    Positive integer option, the attribute is set as a Pymux attribute.
    """

    def __init__(
        self, attribute_name: str, possible_values: Iterable[int] | None = None, scope: Scope = Scope.SESSION
    ) -> None:
        self.attribute_name = attribute_name
        self.possible_values = ["%s" % i for i in (possible_values or [])]
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        try:
            now = getattr(self.held_by(pymux), self._attribute())
        except SetOptionError:
            return sorted(set(self.possible_values))
        return sorted(set([*self.possible_values, "%s" % now]))

    def _read(self, value: str) -> int:
        "The value as a positive integer, or a `SetOptionError`."
        try:
            number = int(value)
            if number < 0:
                raise ValueError
        except ValueError:
            raise SetOptionError("Expecting an integer.")
        return number

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        setattr(self.held_by(pymux, target), self._attribute(), self._read(value))


class PositiveFloatOption(Option):
    "A number above zero, which may have a fraction."

    def __init__(self, attribute_name: str, scope: Scope = Scope.SESSION) -> None:
        self.attribute_name = attribute_name
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        try:
            return ["%g" % getattr(self.held_by(pymux), self._attribute())]
        except SetOptionError:
            return []

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        try:
            number = float(value)
        except ValueError:
            number = 0.0
        if not number > 0 or number == float("inf"):
            raise SetOptionError("Expecting a number above zero.")
        setattr(self.held_by(pymux, target), self._attribute(), number)
        self.after(pymux)

    def after(self, pymux: Pymux) -> None:
        "What the server does once the value has changed."


class AudioRttOption(PositiveFloatOption):
    "`audio-rtt-multiplier`, which the client's audio link hears at once."

    @override
    def after(self, pymux: Pymux) -> None:
        pymux.audio.client_options_changed()


class AudioBitrateOption(PositiveIntOption):
    """
    `audio-speaker-bitrate` or `audio-microphone-bitrate`, in kbit/s,
    which the encoder takes at the next frame. Lillecarl/pymux#674.
    """

    #: What Opus encodes at: 6 to 510 kbit/s.
    LOWEST = 6
    HIGHEST = 510

    def __init__(self, attribute_name: str) -> None:
        super().__init__(attribute_name, scope=Scope.CLIENT)

    @override
    def _read(self, value: str) -> int:
        number = super()._read(value)
        if not self.LOWEST <= number <= self.HIGHEST:
            raise SetOptionError("Expecting kbit/s from %d to %d." % (self.LOWEST, self.HIGHEST))
        return number

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        super().set_value(pymux, value, target)
        pymux.audio.client_options_changed()


class KeyPrefixOption(Option):
    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return PYMUX_TO_PROMPT_TOOLKIT_KEYS.keys()

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        # Translate prefix to prompt_toolkit
        try:
            keys = key_however_it_is_written(value)
        except ValueError:
            raise SetOptionError("Invalid key: %r" % (value,))
        else:
            pymux.key_bindings_manager.prefix = keys
            pymux.key_bindings_manager.prefix_written = value


class EnumOption(Option):
    """
    One of the values of a `StrEnum`, held as the member itself.

    `ChoiceOption` below takes a list of words and holds the word. Use
    this one where the code that reads the option asks which value it
    is, and not what it spells.
    """

    def __init__(self, choices: type[StrEnum], attribute_name: str, scope: Scope = Scope.SESSION) -> None:
        self.choices = choices
        self.attribute_name = attribute_name
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return [str(one) for one in self.choices]

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        try:
            chosen = self.choices(value)
        except ValueError:
            raise SetOptionError("Expecting one of: %s." % ", ".join('"%s"' % one for one in self.choices))
        setattr(self.held_by(pymux, target), self._attribute(), chosen)
        self.after(pymux)

    def after(self, pymux: Pymux) -> None:
        "What the server does once the value has changed."


class AudioFrameOption(EnumOption):
    "`audio-frame`, which both ends of the client's sound hear at once."

    def __init__(self, attribute_name: str) -> None:
        super().__init__(AudioFrame, attribute_name, scope=Scope.CLIENT)

    @override
    def after(self, pymux: Pymux) -> None:
        pymux.audio.client_options_changed()


class ClientAudioOption(EnumOption):
    "`audio`, one client's: play, play and record, or neither."

    def __init__(self, attribute_name: str) -> None:
        super().__init__(ClientAudio, attribute_name, scope=Scope.CLIENT)

    @override
    def after(self, pymux: Pymux) -> None:
        pymux.audio.client_options_changed()


class ThemeOption(Option):
    """
    Which colour scheme one client draws with.

    **A client option, because only a client knows its terminal.** Two
    people on one session, one on a light screen and one on a dark
    one, drew the same colours when the server held this.
    Lillecarl/pymux#223.

    The client follows at once. Its application reads its own style on
    every render, so nothing has to be rebuilt; it only has to be
    asked for a frame.

    A name is either one of `THEMES`, or `pygments:<name>` for one of
    the styles pygments carries - forty-nine of them, and anything a
    package installs beside them. Lillecarl/pymux#194. Or
    `base16:<name>` for one of the schemes of the base16 spec,
    Lillecarl/pymux#282.

    Or `nearest`, which is the default: pymux asks the terminal what
    colours it draws with and takes the known theme closest to the
    answer. Lillecarl/pymux#346.
    """

    scope = Scope.CLIENT

    @override
    def get_all_values(self, pymux: Pymux | None) -> Iterable[str]:
        """
        Every name this option takes, `nearest` first because it is
        the default: a person types it to go back to the search after
        trying a theme, and a name that completes is a name they can
        find. The search itself reads this list and skips `nearest`,
        because it is not a theme to be matched to.
        """
        from pymux.style_base16 import names as base16_names
        from pymux.style_pygments import names

        return (
            [NEAREST]
            + sorted(THEMES)
            + ["pygments:%s" % (name,) for name in names()]
            + ["base16:%s" % (name,) for name in base16_names()]
        )

    @override
    def as_written(self, value: object, holder: Holder | Window | None) -> str:
        """
        `nearest` alone says nothing about what is on the screen, so it
        names the theme it found as well. Lillecarl/pymux#346.
        """
        if value != NEAREST:
            return str(value)
        matched = getattr(holder, "matched", None)
        return NEAREST if matched is None else "%s (%s)" % (NEAREST, matched)

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        if value == NEAREST:
            # Not a theme, and not checked against the sources: it is
            # the name for letting the terminal decide.
            setattr(self.held_by(pymux, target), self._attribute(), NEAREST)
            pymux.invalidate(Woke.THEME_WAS_CHOSEN)
            return

        source, _, rest = value.partition(":")
        if source == "pygments":
            from pymux.style_pygments import names

            if rest not in names():
                raise SetOptionError("Expecting the name of a pygments style, like: %s." % (", ".join(names()[:6]),))
        elif source == "base16":
            from pymux.style_base16 import names

            if rest not in names():
                raise SetOptionError("Expecting the name of a base16 scheme, like: %s." % (", ".join(names()[:6]),))
        elif value not in THEMES:
            raise SetOptionError("Expecting one of: %s." % ", ".join(sorted(THEMES)))

        # The name is read before the client, so a name nothing offers
        # is refused the same way whether or not anybody is attached.
        setattr(self.held_by(pymux, target), self._attribute(), value)
        pymux.invalidate(Woke.THEME_WAS_CHOSEN)

    attribute_name = "theme"


#: What the process holds, once for every session. Lillecarl/pymux#757.
ALL_SERVER_OPTIONS = {name: FieldOption(declared, Scope.SERVER) for name, declared in ServerOptions.DECLARED.items()}


ALL_OPTIONS: dict[str, Option] = {
    name: FieldOption(declared, Scope.SESSION, "session_defaults") for name, declared in SessionOptions.DECLARED.items()
}
# The prefix lives in the binding manager, which saves it with the keys.
ALL_OPTIONS["prefix"] = KeyPrefixOption()


#: What one attached terminal holds. Only a client can know these:
#: what colours its terminal draws with, and which way round they are.
#: A client announces the ones its own configuration file names when
#: it attaches, and `set-client-option` changes one while it runs.
#: Lillecarl/pymux#223.
ALL_CLIENT_OPTIONS = {
    # What a person calls this terminal: "desk", "phone", "the big
    # screen". `detach-client -t` and `set-client-option -t` take it,
    # and so does the derived name it is drawn over.
    #
    # **It lasts as long as the attachment.** `pymux attach -n desk`
    # is the durable one, and a configuration file names a machine a
    # person only has one terminal on. A server that remembered a name
    # per terminal would need a rule for forgetting one whose terminal
    # closed months ago. Lillecarl/pymux#340.
    "name": StringOption("name", scope=Scope.CLIENT),
    # Which colour scheme this client draws with. `pymux/style.py`
    # holds them. Lillecarl/pymux#194.
    "theme": ThemeOption(),
    # The theme is the other way round from this terminal: draw it
    # swapped. A correction to a theme, and therefore as much a fact
    # of one terminal as the theme is.
    "swap-light-and-dark-colors": OnOffOption("swap_dark_and_light", scope=Scope.CLIENT),
    # One pane over every cell of this terminal, with nothing that
    # pymux draws for itself. It hides `status` and
    # `pane-border-status` without changing them, so turning it off
    # gives both back.
    #
    # **One terminal's, like the theme.** A person watching on a phone
    # drops the chrome while the person working keeps it. The plane is
    # shared, so it keeps the rows the chrome of any watcher needs and
    # this client draws background in them. Lillecarl/pymux#471.
    "full-screen": OnOffOption("full_screen", scope=Scope.CLIENT),
    # What a scroll does to a Wayland window: "smooth" for a trackpad,
    # "notch" for a mouse wheel. The terminal does not say which it has.
    # Lillecarl/pymux#652.
    "window-scroll": EnumOption(WindowScroll, "window_scroll", scope=Scope.CLIENT),
    # The browser a program on the server drives on this machine, and
    # the profile it runs with: empty for the first Chromium found and a
    # profile pymux keeps. Lillecarl/pymux#653.
    "browser-program": StringOption("browser_program", scope=Scope.CLIENT),
    "browser-profile": StringOption("browser_profile", scope=Scope.CLIENT),
    # Whether this machine plays the panes' sound and lends its
    # microphone, and how many round trips of sound it buffers.
    # Lillecarl/pymux#657.
    "audio": ClientAudioOption("audio"),
    "audio-rtt-multiplier": AudioRttOption("audio_rtt_multiplier", scope=Scope.CLIENT),
    # How much sound a packet carries, in ms: any size Opus encodes.
    "audio-frame": AudioFrameOption("audio_frame"),
    # What the sound costs the link each way, in kbit/s. Lillecarl/pymux#674.
    "audio-speaker-bitrate": AudioBitrateOption("audio_speaker_bitrate"),
    "audio-microphone-bitrate": AudioBitrateOption("audio_microphone_bitrate"),
    # Who saves a file a pane sends to the person: this terminal, or
    # the pymux client on the person's machine. Lillecarl/pymux#743.
    "file-transfer": EnumOption(FileTransfer, "file_transfer", scope=Scope.CLIENT),
}


ALL_WINDOW_OPTIONS = {name: FieldOption(declared, Scope.WINDOW) for name, declared in WindowOptions.DECLARED.items()}
