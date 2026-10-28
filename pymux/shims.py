"""
The programs a pane finds on its PATH in place of the desktop's: an
opener that asks this session to open a URL, and a `notify-send` that
records in its notification hub. Each lives in a directory this server
makes once, before the first pane that wants it starts.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, ClassVar

from pyte.keep import Keep

from .enums import PaneBrowser

if TYPE_CHECKING:
    from .session import OwnOptions

__all__ = ["NOTIFY_SHIM_SCRIPT", "Shims", "browser_shim"]

#: What the `notify-send` shim understands, as a script. The
#: summary and the body reach `pymux notify`, and `-u` picks the
#: urgency. Every other flag of `notify-send` is accepted and
#: ignored -- the hub keeps no icon, category or timeout -- but a
#: flag that takes a value still eats it, so it is never mistaken
#: for the summary. Hand-parsed, the way the shell parses: this
#: runs wherever a pane runs, and BSD `getopt` knows no long
#: flags.
NOTIFY_SHIM_SCRIPT = r"""#!/bin/sh
urgency=normal
while [ $# -gt 0 ]; do
    case "$1" in
    -u|--urgency) urgency="$2"; shift 2 ;;
    --urgency=*) urgency="${1#*=}"; shift ;;
    -t|-i|-c|-h|-a) shift 2 ;;
    --expire-time|--icon|--category|--hint|--app-name) shift 2 ;;
    --expire-time=*|--icon=*|--category=*|--hint=*|--app-name=*) shift ;;
    -p|--print-id|-w|--wait|-\?|--help|-v|--version) shift ;;
    --) shift; break ;;
    *) break ;;
    esac
done
exec pymux notify -u "$urgency" -- "$@"
"""


def browser_shim() -> str | None:
    "`pymux-browser` of this build, beside its Python; None where it is not."
    found = os.path.join(os.path.dirname(sys.executable), "pymux-browser")
    return found if os.access(found, os.X_OK) else shutil.which("pymux-browser")


class Shims:
    "The directories of the shims, made on first need."

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "open_url_dir": Keep.REBUILT,
        "notify_dir": Keep.REBUILT,
    }

    def __init__(self) -> None:
        self.open_url_dir: str | None = None
        self.notify_dir: str | None = None

    def make(self, options: OwnOptions) -> None:
        """
        Make the directories the options of a new pane ask for.

        In this process, before the fork: a directory made in the child
        is made once per pane, and two panes starting together would
        race for it.
        """
        if options.open_url_shim:
            self.make_open_url()
        if options.notify_shim:
            self.make_notify()

    def make_open_url(self) -> None:
        """
        Make the directory that puts the opener of this session on the
        PATH of a pane.

        It holds one script, `pymux-open-url`, which asks this session
        to open what it was given, and a link to it under the name
        `xdg-open`. A program in a pane calls `xdg-open` without
        knowing pymux, and what reads $BROWSER finds the script by
        name too. Nothing answers for `open` on macOS: too much calls
        it for files, and a name that opens files must not open URLs.

        Only a pane that starts afterwards sees it: a running pane
        keeps the PATH it was born with.
        """
        if self.open_url_dir is not None:
            return
        self.open_url_dir = tempfile.mkdtemp(prefix="pymux-open-url-")
        script = os.path.join(self.open_url_dir, "pymux-open-url")
        with open(script, "w") as f:
            f.write('#!/bin/sh\nexec pymux open-url -- "$@"\n')
        os.chmod(script, 0o755)
        os.symlink("pymux-open-url", os.path.join(self.open_url_dir, "xdg-open"))

    def make_notify(self) -> None:
        """
        Make the directory that puts the notifier of this session on
        the PATH of a pane.

        It holds one script, `notify-send`, which records what it was
        given in the notification hub. It shadows the notifier of the
        desktop on purpose: inside a pane a notification belongs to
        the session first.

        Only a pane that starts afterwards sees it: a running pane
        keeps the PATH it was born with.
        """
        if self.notify_dir is not None:
            return
        self.notify_dir = tempfile.mkdtemp(prefix="pymux-notify-")
        script = os.path.join(self.notify_dir, "notify-send")
        with open(script, "w") as f:
            f.write(NOTIFY_SHIM_SCRIPT)
        os.chmod(script, 0o755)

    def put_on_path(self, environment: dict[str, str], options: OwnOptions) -> None:
        "Put the shims on the PATH of a pane, and name the opener in $BROWSER."
        if options.open_url_shim and self.open_url_dir:
            environment["PATH"] = self.open_url_dir + os.pathsep + environment["PATH"]
            environment["BROWSER"] = os.path.join(self.open_url_dir, "pymux-open-url")
        if options.notify_shim and self.notify_dir:
            environment["PATH"] = self.notify_dir + os.pathsep + environment["PATH"]
        shim = browser_shim()
        if shim is not None and options.pane_browser != PaneBrowser.OFF:
            environment["PLAYWRIGHT_MCP_EXECUTABLE_PATH"] = shim
            if options.pane_browser == PaneBrowser.ALL:
                environment["PUPPETEER_EXECUTABLE_PATH"] = shim
