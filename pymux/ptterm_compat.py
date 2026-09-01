"""
Compatibility fixes for ptterm.

These fix terminal emulation details that ptterm upstream doesn't handle
yet. (TODO: upstream these to ptterm.)
"""
from ptterm.screen import BetterScreen

__all__ = [
    "apply_ptterm_compat_fixes",
]


def _report_device_attributes(self, *args, **kwargs) -> None:
    """
    Reply to a Primary Device Attributes query (`ESC [ c`).

    ptterm replies in the Secondary DA format (`>84;0;0c`), which terminals
    like fish ignore. They then wait for a timeout. tmux replies to DA1 with
    a VT220-style answer; we do the same. (The actual attributes are not
    important, most programs only check for a reply.)
    """
    self.write_process_input("\x1b[?62;1;2;6;9;15;22c")


def apply_ptterm_compat_fixes() -> None:
    "Install the fixes. (Safe to call more than once.)"
    if getattr(BetterScreen, "_pymux_patched", False):
        return
    BetterScreen.report_device_attributes = _report_device_attributes
    BetterScreen._pymux_patched = True
