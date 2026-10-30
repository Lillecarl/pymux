"""
All configurable options which can be changed through "set-option" commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from enum import StrEnum
from typing import TYPE_CHECKING, override

from .arrangement import Window
from .client_options import ClientOptions
from .key_mappings import PYMUX_TO_PROMPT_TOOLKIT_KEYS
from .key_spelling import key_however_it_is_written
from .option_fields import Declared, Options
from .server_options import ServerOptions
from .session import OwnOptions
from .session_options import SessionOptions
from .window_options import WindowOptions

if TYPE_CHECKING:
    from pymux.client_state import ClientState
    from pymux.main import Pymux

    #: What an option is written on and read from: a client, one
    #: session's own values, or the server's, which hold the defaults.
    type Holder = ClientState | OwnOptions | Pymux

__all__ = [
    "ALL_CLIENT_OPTIONS",
    "ALL_OPTIONS",
    "ALL_SERVER_OPTIONS",
    "ALL_WINDOW_OPTIONS",
    "FieldOption",
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

    @override
    def as_written(self, value: object, holder: Holder | Window | None) -> str:
        if self.declared.show is not None:
            return self.declared.show(value, holder)
        return super().as_written(value, holder)

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


#: What the process holds, once for every session. Lillecarl/pymux#757.
ALL_SERVER_OPTIONS = {name: FieldOption(declared, Scope.SERVER) for name, declared in ServerOptions.DECLARED.items()}


ALL_OPTIONS: dict[str, Option] = {
    name: FieldOption(declared, Scope.SESSION, "session_defaults") for name, declared in SessionOptions.DECLARED.items()
}
# The prefix lives in the binding manager, which saves it with the keys.
ALL_OPTIONS["prefix"] = KeyPrefixOption()


#: What one attached terminal holds. A client announces the ones its
#: own configuration file names when it attaches, and
#: `set-client-option` changes one while it runs. Lillecarl/pymux#223.
ALL_CLIENT_OPTIONS = {name: FieldOption(declared, Scope.CLIENT) for name, declared in ClientOptions.DECLARED.items()}


ALL_WINDOW_OPTIONS = {name: FieldOption(declared, Scope.WINDOW) for name, declared in WindowOptions.DECLARED.items()}
