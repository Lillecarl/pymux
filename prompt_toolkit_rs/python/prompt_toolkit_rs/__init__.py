"""
Rust kernels for prompt_toolkit's per-cell loops.

`install()` puts each kernel in place of the Python function it
replaces, the way `pyte_rs` does for pyte. prompt-toolkit itself only
names the functions, so its source stays what upstream could take;
the Rust lives here. Lillecarl/pymux#566.
"""

from __future__ import annotations

import os

from prompt_toolkit import renderer
from prompt_toolkit.layout import containers

from . import _native

__all__ = ["changed_spans", "copy_single_width", "install", "installed"]

#: Set to anything but "" or "0", and `install()` installs nothing.
PURE = "PYTERM_PURE"

#: The functions `install()` replaced, by where they live.
_replaced: dict[str, object] = {}

#: `prompt_toolkit.layout.containers._copy_single_width`, in Rust.
copy_single_width = _native.copy_single_width

#: `prompt_toolkit.renderer._changed_spans`, in Rust.
changed_spans = _native.changed_spans


def install() -> bool:
    "Put every kernel in place, unless `PYTERM_PURE` says not to."
    if os.environ.get(PURE, "") not in ("", "0"):
        return False
    if not _replaced:
        _replaced["prompt_toolkit.layout.containers._copy_single_width"] = containers._copy_single_width
        containers._copy_single_width = copy_single_width
        _replaced["prompt_toolkit.renderer._changed_spans"] = renderer._changed_spans
        renderer._changed_spans = changed_spans
    return True


def installed() -> bool:
    return bool(_replaced)
