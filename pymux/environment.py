"""
The environment that a pane runs in.

A pane is not the terminal that the client attached from. It is an
emulator of its own, and it answers the protocol queries for itself.
A program that reads the environment instead of asking therefore picks
the wrong protocol: it sees the name of the outer terminal and uses a
feature that the pane does not have.

That is not a corner case. A file manager that finds `KITTY_WINDOW_ID`
draws its previews with the unicode placeholders of kitty, which the
pane does not draw; the same program, asked properly, uses the plain
kitty placements that the pane does draw.

So a pane does not inherit the variables that name a terminal. What is
left says the truth: `TERM` names what the pane emulates, and the
queries answer the rest.
"""
from typing import MutableMapping

__all__ = [
    "TERMINAL_IDENTITY_VARIABLES",
    "scrub_terminal_identity",
]

#: The variables that name the terminal that a program runs in. Each
#: one is read by at least one program to pick a graphics or keyboard
#: protocol.
TERMINAL_IDENTITY_VARIABLES = (
    # The generic pair. Every terminal below may set it as well.
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    # kitty.
    "KITTY_WINDOW_ID",
    "KITTY_PID",
    "KITTY_INSTALLATION_DIR",
    "KITTY_PUBLIC_KEY",
    # Konsole.
    "KONSOLE_VERSION",
    "KONSOLE_DBUS_SERVICE",
    "KONSOLE_DBUS_SESSION",
    "KONSOLE_DBUS_WINDOW",
    "KONSOLE_PROFILE_NAME",
    # iTerm2.
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    # WezTerm.
    "WEZTERM_EXECUTABLE",
    "WEZTERM_EXECUTABLE_DIR",
    "WEZTERM_PANE",
    "WEZTERM_UNIX_SOCKET",
    # Ghostty.
    "GHOSTTY_RESOURCES_DIR",
    "GHOSTTY_BIN_DIR",
    # Windows Terminal.
    "WT_SESSION",
    "WT_PROFILE_ID",
    # Warp.
    "WARP_HONOR_PS1",
    # Visual Studio Code.
    "VSCODE_INJECTION",
    # Tabby.
    "TABBY_CONFIG_DIRECTORY",
    # VTE based terminals (GNOME Terminal, Tilix, ...).
    "VTE_VERSION",
    # Alacritty.
    "ALACRITTY_WINDOW_ID",
    "ALACRITTY_SOCKET",
    "ALACRITTY_LOG",
    # Terminology.
    "TERMINOLOGY",
    # Contour.
    "CONTOUR_PROFILE",
)


def scrub_terminal_identity(environment: MutableMapping[str, str]) -> None:
    """
    Remove the variables that name the outer terminal, in place.

    A remote control socket of the outer terminal is left alone. It
    names a service, not the terminal that a pane draws in, and a
    program that uses one still reaches the right window.
    """
    for name in TERMINAL_IDENTITY_VARIABLES:
        environment.pop(name, None)
