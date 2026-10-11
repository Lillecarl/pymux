"""
Copied over prompt-toolkit's own tests by `checks.prompt-toolkit-rs-unit`,
so the whole suite runs with the Rust row and the kernels in place.
Installed here, before any test module makes a screen.
Lillecarl/pymux#570.
"""

from __future__ import annotations

import prompt_toolkit_rs

assert prompt_toolkit_rs.install(), "the check asks for the kernels and PYTERM_PURE refuses them"
