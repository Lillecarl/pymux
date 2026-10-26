"""
All configurable options which can be changed through "set-option" commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, override

from pyte.keys import KEY_PROFILES

from . import log
from .arrangement import Window
from .enums import (
    AudioFrame,
    AudioServerMode,
    ClientAudio,
    EditingKeys,
    FileTransfer,
    PaneBrowser,
    WindowScroll,
    Woke,
)
from .key_mappings import PYMUX_TO_PROMPT_TOOLKIT_KEYS
from .key_spelling import key_however_it_is_written
from .layout import Justify
from .nearest import NEAREST
from .option_fields import Declared, Options
from .style import THEMES
from .utils import get_default_shell
from .window_options import WindowOptions

if TYPE_CHECKING:
    from pymux.main import ClientState, Pymux
    from pymux.session import SessionOptions

    #: What an option is written on and read from: a client, one
    #: session's own values, or the server's, which hold the defaults.
    type Holder = ClientState | SessionOptions | Pymux

__all__ = [
    "ALL_CLIENT_OPTIONS",
    "ALL_OPTIONS",
    "ALL_SERVER_OPTIONS",
    "ALL_WINDOW_OPTIONS",
    "Clipboard",
    "ExtendedKeys",
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

    def __init__(self, declared: Declared, scope: Scope) -> None:
        self.declared = declared
        self.scope = scope
        self.attribute_name = declared.attribute

    def _options_of(self, owner: Holder | Window | None) -> Options:
        "The options `owner` holds, which a field option is written on."
        options = getattr(owner, "options", None)
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


class WaylandServerOption(OnOffOption):
    "`wayland-server`, which starts or stops the compositor as it is set."

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        super().set_value(pymux, value, target)
        pymux.wayland.option_changed()


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


class BaseIndexOption(Option):
    "The index the first new window takes."

    "Base index for window numbering."

    attribute_name = "base_index"

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return ["0", "1"]

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        try:
            number = int(value)
        except ValueError:
            raise SetOptionError("Expecting an integer.")
        else:
            setattr(self.held_by(pymux, target), self._attribute(), number)


class RenumberOption(Option):
    """
    Whether closing a window closes the gap it leaves.

    Setting it on closes the gaps that are open already; setting it
    off changes nothing, because no setting opens one.
    Lillecarl/pymux#342.
    """

    attribute_name = "renumber_windows"

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return ["on", "off"]

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        value = value.lower()
        if value not in ("on", "off"):
            raise SetOptionError('Expecting "on" or "off".')
        setattr(self.held_by(pymux, target), self._attribute(), value == "on")
        # A default reaches every session without its own value, so
        # each session that reads "on" now closes its gaps.
        for session in pymux.sessions:
            if session.arrangement.renumber_windows:
                session.arrangement.renumber()


class ExtendedKeys(StrEnum):
    """
    How much of the keyboard a session uses.

    `OFF` is the escape hatch. A program that misbehaves under the
    extended encodings has somewhere to go, and so does a person who
    attaches with a terminal that claims more than it does: the whole
    session steps down for as long as they choose, and steps back up
    without restarting anything.

    `ALWAYS` is for a terminal that speaks the protocol and does not
    answer the query that asks. There are some.
    """

    OFF = "off"
    ON = "on"
    ALWAYS = "always"


class Clipboard(StrEnum):
    """
    Who may write the clipboard of the person at the keyboard.

    `EXTERNAL` is the careful one, and tmux's default: a copy the
    person makes in copy mode goes out to their terminal, and a
    program in a pane cannot reach the clipboard at all.

    `ON` lets a program reach it, which is what a terminal does when
    nothing stands between the two. It is a real choice: a program on
    the other side of an ssh connection can then write what a person
    pastes next.
    """

    OFF = "off"
    EXTERNAL = "external"
    ON = "on"


class OpenUrlTarget(StrEnum):
    """
    Which client's browser "open-url" opens in.

    `LAST` is the client somebody used last, by the stamp that
    `window-size latest` reads. `BROADCAST` is every attached client.
    """

    LAST = "last"
    BROADCAST = "broadcast"


class OpenUrlMode(StrEnum):
    """
    What happens to a request to open a URL.

    `ASK` shows "(y/n)" in the command bar of the clients it would
    land on, and a yes runs `open-url -c`, so the question is asked
    once.
    """

    OPEN = "open"
    ASK = "ask"
    OFF = "off"


class ForwardMode(StrEnum):
    """
    What happens to a request to forward a port.

    **`ON` is not "never ask".** A request that binds somewhere other
    than loopback, and one that a program in a pane made rather than
    the person, are confirmed whatever this says: `Pymux.forward_needs_asking`
    holds the two. The port belongs to the machine somebody is sitting
    at. Lillecarl/pymux#440.
    """

    ON = "on"
    ASK = "ask"
    OFF = "off"


class RenderMode(StrEnum):
    """
    How a client's frames reach its terminal.

    `OPTIMIZED` writes what changed since the frame before, scrolls
    rows on the wire instead of repainting them, and builds a pane's
    rows only when they changed. `REFERENCE` writes every cell of
    every frame at its own position and keeps nothing between frames.
    It is slow on purpose: it is the answer the optimized frames are
    held against, and the way round a fault in them until it is fixed.
    """

    OPTIMIZED = "optimized"
    REFERENCE = "reference"


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


class AudioServerOption(EnumOption):
    "`audio-server`, which stops PipeWire and starts it again as it says."

    def __init__(self, attribute_name: str) -> None:
        super().__init__(AudioServerMode, attribute_name, scope=Scope.SERVER)

    @override
    def after(self, pymux: Pymux) -> None:
        pymux.audio.option_changed()


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


class ExtendedKeysOption(EnumOption):
    "How much of the keyboard a session uses."

    def __init__(self, attribute_name: str, scope: Scope = Scope.SESSION) -> None:
        super().__init__(ExtendedKeys, attribute_name, scope)

    @override
    def after(self, pymux: Pymux) -> None:
        # The server tells every client what its terminal should send
        # now, whoever holds the value.
        pymux.sync_keyboard()


class RenderModeOption(EnumOption):
    "How every client's frames reach its terminal."

    def __init__(self, attribute_name: str) -> None:
        super().__init__(RenderMode, attribute_name, scope=Scope.SERVER)

    @override
    def after(self, pymux: Pymux) -> None:
        pymux.apply_render_mode()


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


class LogLevelOption(Option):
    """
    How much this server writes to its log, from now on.

    It reaches a server that is already running, which is the only time
    it is any use: a person turns debug logging on because something is
    already wrong, and a restart takes the thing they wanted to look at
    with it. Lillecarl/pymux#252.
    """

    scope = Scope.SERVER
    attribute_name = "log_level"

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return sorted(log.LEVELS)

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        if value not in log.LEVELS:
            raise SetOptionError("Expecting one of: %s." % ", ".join(sorted(log.LEVELS)))
        pymux.log_level = value


class JustifyOption(Option):
    def __init__(self, attribute_name: str, scope: Scope = Scope.SESSION) -> None:
        self.attribute_name = attribute_name
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        return Justify._ALL

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        if value not in Justify._ALL:
            raise SetOptionError("Invalid justify option.")
        setattr(self.held_by(pymux, target), self._attribute(), value)


class ChoiceOption(Option):
    """
    An option whose value is one word of a few.

    `StringOption` takes anything; this one takes the words it was
    given, and nothing else. A word that is not one of them would
    change nothing and say nothing, which is the worst answer a
    configuration mistake can get.
    """

    def __init__(self, attribute_name: str, choices: Iterable[str], scope: Scope = Scope.SESSION) -> None:
        self.attribute_name = attribute_name
        self.choices = tuple(choices)
        self.scope = scope

    @override
    def get_all_values(self, pymux: Pymux) -> Iterable[str]:
        try:
            now = getattr(self.held_by(pymux), self._attribute())
        except SetOptionError:
            return sorted(set(self.choices))
        return sorted(set((*self.choices, now)))

    @override
    def set_value(self, pymux: Pymux, value: str, target: Holder | None = None) -> None:
        if value not in self.choices:
            raise SetOptionError("Expecting one of: %s." % ", ".join(sorted(self.choices)))
        setattr(self.held_by(pymux, target), self._attribute(), value)


#: What the process holds, once for every session. Lillecarl/pymux#757.
ALL_SERVER_OPTIONS = {
    # Whether another process of this user may attach a debugger to this
    # server: `python -m pdb -p <pid>`, or py-spy. Off, and it stays off
    # unless a person asks: it lets anything they run read this server's
    # memory, and a server holds every pane's scrollback.
    # `pymux/introspect.py` says what answers most questions without it.
    "allow-remote-debugging": OnOffOption("allow_remote_debugging", scope=Scope.SERVER),
    # How much this server writes to its log, changed while it runs.
    # `--log-level` says the same thing before it starts.
    "log-level": LogLevelOption(),
    # Who may write the clipboard of the person at the keyboard.
    # "external" is tmux's default: a copy the person makes goes out,
    # and a program in a pane is refused. Lillecarl/pymux#378.
    "set-clipboard": EnumOption(Clipboard, "clipboard_mode", scope=Scope.SERVER),
    # Hold a Wayland compositor for the panes whose session has no
    # display, so wl-copy and wl-paste work there. Needs the `wayland`
    # extra (pysmithay, Linux); without it this does nothing. A pane
    # that runs already keeps the environment it was born with.
    # Lillecarl/pymux#383.
    "wayland-server": WaylandServerOption("wayland_server", scope=Scope.SERVER),
    # Whose PipeWire holds the speaker and microphone that panes play
    # into and record from: "private", "shared", "auto" or "off".
    # `AudioServerMode` says what each means. Only new panes see it.
    # Lillecarl/pymux#657.
    "audio-server": AudioServerOption("audio_server"),
    # What happens to a request to forward a port, and the two
    # cases that ask whatever it says. `ForwardMode` says.
    "forward-mode": EnumOption(ForwardMode, "forward_mode", scope=Scope.SERVER),
    # How long a job outlives its last activity, in seconds. Output
    # and completion both count, so a job that logs stays while a job
    # that hangs silent goes; a running one is ended first, a
    # finished one just forgotten, and a waited one never. Zero keeps
    # every job until the cap forgets the finished ones.
    "job-ttl": PositiveIntOption("job_ttl", [0, 600, 3600, 86400], scope=Scope.SERVER),
    # Write what a person does to `actions.db` under the state
    # directory: each command, what started it -- a key, the command
    # bar, a click, a hook -- and the clicks that run none. Never what a
    # pane shows or what is typed into one. Off, because nobody expects
    # a multiplexer to keep that. `pymux/actions.py` says the rest.
    "action-log": OnOffOption("action_log", scope=Scope.SERVER),
    # The most rows `actions.db` keeps; the oldest go first. Zero keeps all.
    "action-log-limit": PositiveIntOption("action_log_limit", [0, 100_000, 500_000, 1_000_000], scope=Scope.SERVER),
    "default-terminal": StringOption("default_terminal", ["xterm", "xterm-256color", "screen"], scope=Scope.SERVER),
    # How much of the keyboard this server uses. "off" is the escape
    # hatch: for a program that misbehaves under the extended
    # encodings, and for a person attaching with a terminal that
    # claims more than it does. Lillecarl/pymux#173.
    "extended-keys": ExtendedKeysOption("extended_keys", scope=Scope.SERVER),
    # "reference" draws every cell of every frame and keeps nothing,
    # which is slow and right. `RenderMode` says why it exists.
    "render-mode": RenderModeOption("render_mode"),
    # Show 13:37 on the 14th of March, of the year it really is,
    # everywhere pymux displays a clock: the status line's format
    # strings, a pane in clock-mode, and the frame token that says
    # when a clock changed. Off by default. The point is a screen that
    # does not change while nobody types: a picture of a pane cannot
    # race the clock. Lillecarl/pymux#273's galleries photograph them.
    "test-mode": OnOffOption("test_mode", scope=Scope.SERVER),
}


ALL_OPTIONS = {
    "base-index": BaseIndexOption(),
    # Whether closing a window packs the ones above it down, so the
    # order has no gaps. Off, the way tmux ships it.
    # Lillecarl/pymux#342.
    "renumber-windows": RenumberOption(),
    "bell": OnOffOption("enable_bell"),
    # Which client's browser it lands on. `OpenUrlTarget` says.
    "open-url-target": EnumOption(OpenUrlTarget, "open_url_target"),
    # What happens to an open request. `OpenUrlMode` says.
    "open-url-mode": EnumOption(OpenUrlMode, "open_url_mode"),
    # Put the opener of this session on the PATH of a pane, as
    # "xdg-open", and name it in $BROWSER, so that a program in a pane
    # opens a browser here without knowing pymux. A pane that runs
    # already keeps the PATH it was born with; only new panes see it.
    "open-url-shim": OnOffOption("open_url_shim"),
    # Which programs in a pane find `pymux-browser` as their browser:
    # "agents" (Playwright MCP and playwright-cli), "all" (Puppeteer
    # too), or "off". `PaneBrowser` says why the default is "agents".
    # A pane that runs already keeps the environment it was born with.
    "pane-browser": EnumOption(PaneBrowser, "pane_browser"),
    # Put the notifier of this session on the PATH of a pane, as
    # "notify-send", so that a program in a pane notifies the hub
    # without knowing pymux. It shadows the notifier of the desktop:
    # inside a pane a notification belongs to the session first, and
    # the hub is where it waits until something emits it. A pane that
    # runs already keeps the PATH it was born with; only new panes
    # see it.
    "notify-shim": OnOffOption("notify_shim"),
    # Bring the port with the URL. A "localhost" address opened on the
    # browser of the machine at the keyboard means a service on that
    # machine, which is the wrong one, so the client forwards the port
    # first. Lillecarl/pymux#437.
    "open-url-forward": OnOffOption("open_url_forward"),
    # How long such a forward outlives its last connection, in seconds.
    # A forward carrying a connection is never idle, however quiet it
    # is, so this measures a page nobody has open. Opening the URL
    # again brings a reaped forward back.
    "open-url-forward-idle": PositiveIntOption("open_url_forward_idle", [60, 300, 600, 3600]),
    "history-limit": PositiveIntOption("history_limit", [200, 500, 1000, 2000, 5000, 10000]),
    # How long a strip's view takes to glide to the column a person
    # moved to, in milliseconds. Zero jumps.
    "strip-animation-time": PositiveIntOption("strip_animation_time", [0, 100, 150, 250]),
    # The shade at the edge where the view cuts a pane: how many cells
    # it fades over, zero for none, and how far the cell at the edge
    # moves towards black or white, in percent. Lillecarl/pymux#616.
    "cut-shade-width": PositiveIntOption("cut_shade_width", [0, 2, 4, 6, 8]),
    "cut-shade-strength": PositiveIntOption("cut_shade_strength", [8, 12, 16, 24]),
    "mouse": OnOffOption("enable_mouse_support"),
    "prefix": KeyPrefixOption(),
    "remain-on-exit": OnOffOption("remain_on_exit"),
    "status": OnOffOption("enable_status"),
    "pane-border-status": OnOffOption("enable_pane_status"),
    # **`full-screen` is not here.** It hides the two options above,
    # and it belongs to one terminal, so it is in
    # `ALL_CLIENT_OPTIONS`. Lillecarl/pymux#471.
    # Draw the ":" command line as a box in the middle of the screen
    # instead of a bar along the bottom. Off, because a person used to
    # the bar should not have it move without asking.
    "command-palette": OnOffOption("command_palette"),
    # Draw the keys a prefix leads to in a popup while the prefix
    # waits. Off, because a person who knows the keys does not want a
    # popup in the way. Lillecarl/pymux#29.
    "which-key": OnOffOption("which_key"),
    # The window chooser starts each session on a line of its own, so
    # the sessions read as groups. Lillecarl/pymux#531.
    "chooser-session-lines": OnOffOption("chooser_session_lines"),
    "status-keys": EnumOption(EditingKeys, "status_keys"),
    "mode-keys": EnumOption(EditingKeys, "mode_keys"),
    # What lock-server, lock-session and lock-client run to hold the
    # keyboard. tmux's default is vlock.
    "lock-command": StringOption("lock_command", ["vlock"]),
    "status-right": StringOption("status_right"),
    "status-left": StringOption("status_left"),
    "status-right-length": PositiveIntOption("status_right_length", [40]),
    "status-left-length": PositiveIntOption("status_left_length", [20]),
    # The most rows the window list wraps to when it does not fit on
    # one. A window past the last row is cut. Lillecarl/pymux#723.
    "status-max-rows": PositiveIntOption("status_max_rows", [1, 2, 3, 5]),
    "window-status-format": StringOption("window_status_format"),
    "window-status-current-format": StringOption("window_status_current_format"),
    # What `#F` writes for each mark of a window. ASCII, so every font
    # has them; a configuration may set icons. Lillecarl/pymux#767.
    "bell-symbol": StringOption("bell_symbol"),
    "activity-symbol": StringOption("activity_symbol"),
    "silence-symbol": StringOption("silence_symbol"),
    "notification-symbol": StringOption("notification_symbol"),
    "default-shell": StringOption("default_shell", [get_default_shell()]),
    "status-justify": JustifyOption("status_justify"),
    "status-interval": PositiveIntOption("status_interval", [1, 2, 4, 8, 16, 30, 60]),
    # The names a client brings with it, space separated, each one a
    # pattern. `Pymux.take_environment_from` says what happens to them.
    # Lillecarl/pymux#271.
    "update-environment": StringOption("update_environment"),
    # Make up the halves of a key event that the keyboard of a client
    # cannot send, so that a pane gets the keyboard protocol whole from
    # any terminal.
    "synthesize-key-events": OnOffOption("synthesize_key_events"),
    # Whose answer a program in the legacy encoding gets where the
    # terminals split, for a client whose terminal pymux cannot name.
    # A terminal that names itself gets its own. Lillecarl/pymux#706.
    "key-profile": ChoiceOption("key_profile", KEY_PROFILES),
    # May a program inside a pane resize that pane? Off by default: a
    # pane sits in a layout, and making one taller makes another
    # shorter.
    "allow-program-resize": OnOffOption("allow_program_resize"),
    # Draw the theme's own background behind every cell of a pane, so
    # the terminal's background is never seen and a theme is the
    # colour of the whole screen. Off by default: a program that
    # relies on the terminal's background through its default cells --
    # an image viewer, a sixel client -- keeps seeing the terminal
    # while this is off, and the terminal's ANSI palette stays the
    # terminal's either way. A client that asks the terminal its
    # background (OSC 11, Lillecarl/pymux#223) is the softer cousin of
    # the same idea; with this on, the asking is unnecessary rather
    # than wrong. The panes answer a program's colour queries with
    # the theme's sixteen as well, so a theme is one colour scheme
    # from the chrome to the palette. Lillecarl/pymux#273,
    # Lillecarl/pymux#283.
    "paint-screen": OnOffOption("paint_screen"),
}


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
