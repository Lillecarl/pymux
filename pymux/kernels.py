"""
The Rust kernels, in place of the Python loops they replace.

`pymux.main` calls `install()` when it is imported, which is before
anything draws and after the toolkit is loaded anyway. Not in the
package's `__init__`: `prompt_toolkit_rs` imports prompt_toolkit, and a
detached command such as `pymux ls` must not pay for the toolkit.
`PYTERM_PURE=1` keeps the Python, and so does a build without the
kernels: the answers are the same either way. Lillecarl/pymux#566.
"""

from __future__ import annotations

__all__ = ["install"]


def install() -> None:
    try:
        import pyte_rs
    except ImportError:
        pass
    else:
        pyte_rs.install()

    try:
        import prompt_toolkit_rs
    except ImportError:
        pass
    else:
        prompt_toolkit_rs.install()
