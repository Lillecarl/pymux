"""
Rust kernels for prompt_toolkit's per-cell loops, and a Rust row for its
composed screen.

`install()` puts each kernel in place of the Python function it
replaces, the way `pyte_rs` does for pyte. prompt-toolkit itself only
names the functions, so its source stays what upstream could take;
the Rust lives here. Lillecarl/pymux#566, Lillecarl/pymux#570.
"""

from __future__ import annotations

import os

from prompt_toolkit import renderer
from prompt_toolkit.layout import containers, screen

from . import _native

__all__ = [
    "Row",
    "changed_spans",
    "copy_columns",
    "copy_single_width",
    "install",
    "installed",
    "last_column",
    "queue_same_style",
    "store_line",
]

#: Set to anything but "" or "0", and `install()` installs nothing.
PURE = "PYTERM_PURE"

#: The functions `install()` replaced, by where they live.
_replaced: dict[str, object] = {}

#: A row of `prompt_toolkit.layout.screen.Screen`, in place of
#: `screen._new_row`'s defaultdict. `Row(default)` makes one.
Row = _native.Row

#: `prompt_toolkit.layout.containers._copy_single_width`, in Rust.
copy_single_width = _native.copy_single_width

#: `prompt_toolkit.layout.containers._store_line`, in Rust.
store_line = _native.store_line

#: `prompt_toolkit.layout.screen._last_column`, in Rust.
last_column = _native.last_column

#: `prompt_toolkit.layout.screen._copy_columns`, in Rust.
copy_columns = _native.copy_columns

#: `prompt_toolkit.renderer._changed_spans`, in Rust.
changed_spans = _native.changed_spans

#: `prompt_toolkit.renderer._queue_same_style`, in Rust.
queue_same_style = _native.queue_same_style

#: What `install()` puts where: module, name, replacement.
_KERNELS = (
    (screen, "_new_row", Row),
    (screen, "_last_column", last_column),
    (screen, "_copy_columns", copy_columns),
    (containers, "_copy_single_width", copy_single_width),
    (containers, "_store_line", store_line),
    (renderer, "_changed_spans", changed_spans),
    (renderer, "_queue_same_style", queue_same_style),
)


def install() -> bool:
    "Put every kernel in place, unless `PYTERM_PURE` says not to."
    if os.environ.get(PURE, "") not in ("", "0"):
        return False
    if not _replaced:
        for module, name, kernel in _KERNELS:
            _replaced[f"{module.__name__}.{name}"] = getattr(module, name)
            setattr(module, name, kernel)
    return True


def installed() -> bool:
    return bool(_replaced)
