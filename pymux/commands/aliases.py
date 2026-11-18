"""
Aliases for all commands.

Every `.alias` of a `cmd_entry` in tmux's source whose command pymux
has, so a tmux configuration reads here. `run` and `wait` are tmux's
aliases of `run-shell` and `wait-for`, and pymux's own commands here:
Lillecarl/pyterm#1028.
"""

from __future__ import annotations

__all__ = ["ALIASES"]


ALIASES = {
    "attach": "attach-session",
    "bind": "bind-key",
    "breakp": "break-pane",
    "capturep": "capture-pane",
    "clearhist": "clear-history",
    "clearphist": "clear-prompt-history",
    "confirm": "confirm-before",
    "deleteb": "delete-buffer",
    "detach": "detach-client",
    "display": "display-message",
    "displayp": "display-panes",
    "has": "has-session",
    "if": "if-shell",
    "joinp": "join-pane",
    "killp": "kill-pane",
    "killw": "kill-window",
    "last": "last-window",
    "lastp": "last-pane",
    "linkw": "link-window",
    "loadb": "load-buffer",
    "lock": "lock-server",
    "lockc": "lock-client",
    "locks": "lock-session",
    "lsb": "list-buffers",
    "lsc": "list-clients",
    "lscm": "list-commands",
    "lsk": "list-keys",
    "lsp": "list-panes",
    "lsw": "list-windows",
    "menu": "display-menu",
    "movep": "move-pane",
    "movew": "move-window",
    "new": "new-session",
    "newp": "new-pane",
    "neww": "new-window",
    "next": "next-window",
    "nextl": "next-layout",
    "pasteb": "paste-buffer",
    "popup": "display-popup",
    "prev": "previous-window",
    "prevl": "previous-layout",
    "refresh": "refresh-client",
    "rename": "rename-session",
    "renamew": "rename-window",
    "resizep": "resize-pane",
    "resizew": "resize-window",
    "respawnp": "respawn-pane",
    "respawnw": "respawn-window",
    "rotatew": "rotate-window",
    "saveb": "save-buffer",
    "selectl": "select-layout",
    "selectp": "select-pane",
    "selectw": "select-window",
    "send": "send-keys",
    "set": "set-option",
    "setb": "set-buffer",
    "setenv": "set-environment",
    "setw": "set-window-option",
    # Not a tmux alias: tmux has no client options. It follows the
    # shape of the two above, which is what a person will reach for.
    "setc": "set-client-option",
    "show": "show-options",
    "showb": "show-buffer",
    "showenv": "show-environment",
    "showmsgs": "show-messages",
    "showphist": "show-prompt-history",
    "showw": "show-window-options",
    "source": "source-file",
    "splitw": "split-window",
    "suspendc": "suspend-client",
    "swapp": "swap-pane",
    "swapw": "swap-window",
    "switchc": "switch-client",
    "unbind": "unbind-key",
    "unlinkw": "unlink-window",
}
