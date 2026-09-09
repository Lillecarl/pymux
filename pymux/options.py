"""
All configurable options which can be changed through "set-option" commands.
"""

from abc import ABC, abstractmethod
from enum import StrEnum

from .enums import WindowSize, Woke
from .key_mappings import (
    PYMUX_TO_PROMPT_TOOLKIT_KEYS,
    pymux_key_to_prompt_toolkit_key_sequence,
)
from .layout import Justify
from .style import THEMES
from .utils import get_default_shell

__all__ = [
    "Option",
    "SetOptionError",
    "OnOffOption",
    "ExtendedKeys",
    "ALL_OPTIONS",
    "ALL_WINDOW_OPTIONS",
]


class Option(ABC):
    """
    Base class for all options.
    """

    @abstractmethod
    def get_all_values(self):
        """
        Return a list of strings, with all possible values. (For
        autocompletion.)
        """

    @abstractmethod
    def set_value(self, pymux, value):
        "Set option. This can raise SetOptionError."

    def set_default(self, pymux, value):
        """
        Say what a new window starts with, without changing one.

        `set-window-option -g` reaches this, and only a window option
        has anything to say: a session option is already one value for
        the whole session. Lillecarl/pymux#199.
        """
        raise SetOptionError(
            "This option belongs to the session, so it is already global."
        )


class SetOptionError(Exception):
    """
    Raised when setting an option fails.
    """

    def __init__(self, message):
        self.message = message


class OnOffOption(Option):
    """
    Boolean on/off option.
    """

    def __init__(self, attribute_name, window_option=False):
        self.attribute_name = attribute_name
        self.window_option = window_option

    def get_all_values(self, pymux):
        return ["on", "off"]

    def _read(self, value):
        "The value as a boolean, or a `SetOptionError`."
        value = value.lower()
        if value not in ("on", "off"):
            raise SetOptionError('Expecting "yes" or "no".')
        return value == "on"

    def set_value(self, pymux, value):
        chosen = self._read(value)

        if self.window_option:
            # There may be no window. A configuration file is read
            # before the first one is made, so `set-window-option` in
            # one asked the arrangement for a window it did not have
            # and got an `IndexError` out of `windows[0]`. That is not
            # a `SetOptionError`, so it left `source-file` and took the
            # startup with it: pymux drew nothing at all.
            # Lillecarl/pymux#199.
            if not pymux.arrangement.windows:
                raise SetOptionError(
                    "There is no window yet. A window option belongs to one "
                    "window, so a configuration file has none to set. "
                    'Use "-g" to say what every new window starts with.'
                )
            w = pymux.arrangement.get_active_window()
            setattr(w, self.attribute_name, chosen)
        else:
            setattr(pymux, self.attribute_name, chosen)

    def set_default(self, pymux, value):
        "What every new window starts with. Changes no window that is open."
        if not self.window_option:
            return super().set_default(pymux, value)

        pymux.arrangement.window_defaults[self.attribute_name] = self._read(value)


class StringOption(Option):
    """
    String option, the attribute is set as a Pymux attribute.
    """

    def __init__(self, attribute_name, possible_values=None):
        self.attribute_name = attribute_name
        self.possible_values = possible_values or []

    def get_all_values(self, pymux):
        return sorted(set(self.possible_values + [getattr(pymux, self.attribute_name)]))

    def set_value(self, pymux, value):
        setattr(pymux, self.attribute_name, value)


class PositiveIntOption(Option):
    """
    Positive integer option, the attribute is set as a Pymux attribute.
    """

    def __init__(self, attribute_name, possible_values=None):
        self.attribute_name = attribute_name
        self.possible_values = ["%s" % i for i in (possible_values or [])]

    def get_all_values(self, pymux):
        return sorted(
            set(self.possible_values + ["%s" % getattr(pymux, self.attribute_name)])
        )

    def set_value(self, pymux, value):
        """
        Take a string, and return an integer. Raise SetOptionError when the
        given text does not parse to a positive integer.
        """
        try:
            value = int(value)
            if value < 0:
                raise ValueError
        except ValueError:
            raise SetOptionError("Expecting an integer.")
        else:
            setattr(pymux, self.attribute_name, value)


class KeyPrefixOption(Option):
    def get_all_values(self, pymux):
        return PYMUX_TO_PROMPT_TOOLKIT_KEYS.keys()

    def set_value(self, pymux, value):
        # Translate prefix to prompt_toolkit
        try:
            keys = pymux_key_to_prompt_toolkit_key_sequence(value)
        except ValueError:
            raise SetOptionError("Invalid key: %r" % (value,))
        else:
            pymux.key_bindings_manager.prefix = keys


class BaseIndexOption(Option):
    "Base index for window numbering."

    def get_all_values(self, pymux):
        return ["0", "1"]

    def set_value(self, pymux, value):
        try:
            value = int(value)
        except ValueError:
            raise SetOptionError("Expecting an integer.")
        else:
            pymux.arrangement.base_index = value


class KeysOption(Option):
    "Emacs or Vi mode."

    def __init__(self, attribute_name):
        self.attribute_name = attribute_name

    def get_all_values(self, pymux):
        return ["emacs", "vi"]

    def set_value(self, pymux, value):
        if value in ("emacs", "vi"):
            setattr(pymux, self.attribute_name, value == "vi")
        else:
            raise SetOptionError('Expecting "vi" or "emacs".')


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


class ExtendedKeysOption(Option):
    "How much of the keyboard a session uses."

    def __init__(self, attribute_name):
        self.attribute_name = attribute_name

    def get_all_values(self, pymux):
        return [str(one) for one in ExtendedKeys]

    def set_value(self, pymux, value):
        try:
            chosen = ExtendedKeys(value)
        except ValueError:
            raise SetOptionError(
                "Expecting one of: %s."
                % ", ".join('"%s"' % one for one in ExtendedKeys)
            )
        setattr(pymux, self.attribute_name, chosen)
        pymux.sync_the_keyboard()


class WindowSizeOption(Option):
    """
    Which client's terminal decides how big a window's plane is.

    A window option, because two windows of one session can be watched
    by different clients. Decision 11 of `docs/layout-engine-plan.md`.
    """

    def get_all_values(self, pymux):
        return [str(one) for one in WindowSize]

    def _read(self, value):
        try:
            return WindowSize(value)
        except ValueError:
            raise SetOptionError(
                "Expecting one of: %s." % ", ".join('"%s"' % one for one in WindowSize)
            ) from None

    def set_value(self, pymux, value):
        chosen = self._read(value)

        if not pymux.arrangement.windows:
            raise SetOptionError(
                "There is no window yet. A window option belongs to one "
                "window, so a configuration file has none to set. "
                'Use "-g" to say what every new window starts with.'
            )

        window = pymux.arrangement.get_active_window()

        if chosen is WindowSize.MANUAL and window.manual_size is None:
            # **`manual` with no size freezes the window as it is.** A
            # person who says it and nothing else means "stop following
            # the clients", not "pick a size for me", and tmux does the
            # same. `resize-window` is how a size is named.
            window.manual_size = pymux.the_size_of_the_plane(window)

        window.window_size = chosen

    def set_default(self, pymux, value):
        "What every new window starts with. Changes no window that is open."
        pymux.arrangement.window_defaults["window_size"] = self._read(value)


class ThemeOption(Option):
    """
    Which colour scheme every client draws with.

    The clients follow at once. Each application reads `pymux.style`
    on every render, so nothing has to be rebuilt; they only have to
    be asked for a frame.
    """

    def get_all_values(self, pymux):
        return sorted(THEMES)

    def set_value(self, pymux, value):
        if value not in THEMES:
            raise SetOptionError("Expecting one of: %s." % ", ".join(sorted(THEMES)))
        pymux.theme = value
        pymux.invalidate(Woke.A_THEME_WAS_CHOSEN)


class JustifyOption(Option):
    def __init__(self, attribute_name):
        self.attribute_name = attribute_name

    def get_all_values(self, pymux):
        return Justify._ALL

    def set_value(self, pymux, value):
        if value in Justify._ALL:
            setattr(pymux, self.attribute_name, value)
        else:
            raise SetOptionError("Invalid justify option.")


ALL_OPTIONS = {
    "base-index": BaseIndexOption(),
    "bell": OnOffOption("enable_bell"),
    "set-clipboard": OnOffOption("enable_clipboard"),
    "history-limit": PositiveIntOption(
        "history_limit", [200, 500, 1000, 2000, 5000, 10000]
    ),
    "mouse": OnOffOption("enable_mouse_support"),
    "prefix": KeyPrefixOption(),
    "remain-on-exit": OnOffOption("remain_on_exit"),
    "status": OnOffOption("enable_status"),
    "pane-border-status": OnOffOption("enable_pane_status"),
    # One pane over every cell, with nothing that pymux draws for
    # itself. It hides the two options above without changing them.
    "full-screen": OnOffOption("full_screen"),
    # Draw the ":" command line as a box in the middle of the screen
    # instead of a bar along the bottom. Off, because a person used to
    # the bar should not have it move without asking.
    "command-palette": OnOffOption("command_palette"),
    # Which colour scheme the clients draw with. `pymux/style.py`
    # holds them. Lillecarl/pymux#194.
    "theme": ThemeOption(),
    "status-keys": KeysOption("status_keys_vi_mode"),
    "mode-keys": KeysOption("mode_keys_vi_mode"),
    "default-terminal": StringOption(
        "default_terminal", ["xterm", "xterm-256color", "screen"]
    ),
    "status-right": StringOption("status_right"),
    "status-left": StringOption("status_left"),
    "status-right-length": PositiveIntOption("status_right_length", [20]),
    "status-left-length": PositiveIntOption("status_left_length", [20]),
    "window-status-format": StringOption("window_status_format"),
    "window-status-current-format": StringOption("window_status_current_format"),
    "default-shell": StringOption("default_shell", [get_default_shell()]),
    "status-justify": JustifyOption("status_justify"),
    "status-interval": PositiveIntOption("status_interval", [1, 2, 4, 8, 16, 30, 60]),
    # Make up the halves of a key event that the keyboard of a client
    # cannot send, so that a pane gets the keyboard protocol whole from
    # any terminal.
    "synthesize-key-events": OnOffOption("synthesize_key_events"),
    # How much of the keyboard this session uses. "off" is the escape
    # hatch: for a program that misbehaves under the extended
    # encodings, and for a person attaching with a terminal that
    # claims more than it does. Lillecarl/pymux#173.
    "extended-keys": ExtendedKeysOption("extended_keys"),
    # May a program inside a pane resize that pane? Off by default: a
    # pane sits in a layout, and making one taller makes another
    # shorter.
    "allow-program-resize": OnOffOption("allow_program_resize"),
    # Prompt-toolkit/pymux specific.
    "swap-light-and-dark-colors": OnOffOption("swap_dark_and_light"),
}


ALL_WINDOW_OPTIONS = {
    "synchronize-panes": OnOffOption("synchronize_panes", window_option=True),
    # Lay this window's panes out as a strip that may run past the
    # edge of the screen, the way niri's scrollable tiling works,
    # instead of dividing the window between them.
    #
    # It is one window's option and it is off, so nothing that does
    # not ask for it changes. `select-layout` turns it off again.
    # Lillecarl/pymux#198.
    "strip": OnOffOption("strip", window_option=True),
    # Which client's terminal decides how big this window's plane is,
    # when more than one watches it. "smallest" is what pymux always
    # did: every client sees the whole window, and a bigger one draws
    # background around it. "largest" gives the window to the biggest
    # client, and a smaller one moves its own view over it.
    # Lillecarl/pymux#217.
    "window-size": WindowSizeOption(),
}
