"""
The files a server sends this client to save: a pane's download, when
`file-transfer` says the pymux client saves it. Lillecarl/pymux#743.

`pymux.files` joins the parts and writes the file. This module picks
the directory on this machine.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ..files import Whole, save

__all__ = [
    "DOWNLOAD_DIR_VARIABLE",
    "download_directory",
    "save_whole",
]

#: The variable that names the person's downloads directory.
DOWNLOAD_DIR_VARIABLE: Final = "XDG_DOWNLOAD_DIR"

#: The directory under the home directory that holds downloads when the
#: variable names none.
DOWNLOADS: Final = "Downloads"


def download_directory(environment: Mapping[str, str], home: Path, current: Path) -> Path:
    """
    Where a download goes: `XDG_DOWNLOAD_DIR`, then `~/Downloads`, else
    the current directory. A directory that is not there is skipped.
    """
    named = environment.get(DOWNLOAD_DIR_VARIABLE, "")
    if named:
        # user-dirs.dirs writes it as "$HOME/Downloads".
        path = Path(named.replace("$HOME", str(home))).expanduser()
        if path.is_dir():
            return path
    downloads = home / DOWNLOADS
    if downloads.is_dir():
        return downloads
    return current


def save_whole(whole: Whole) -> str:
    """
    Write a file that arrived whole, and say where it went or why not.

    **Never on a loop that draws.** 64 MiB stopped drawing and typing
    for as long as the write took. Lillecarl/pymux#747.
    """
    directory = download_directory(os.environ, Path.home(), Path.cwd())
    try:
        path = save(directory, whole.name, whole.data)
    except OSError as error:
        return "Could not save %s: %s" % (whole.name, error)
    return "Saved %s." % (path,)
