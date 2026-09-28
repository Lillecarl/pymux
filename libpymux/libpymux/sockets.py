"""
Where a pymux server's socket is.

The room and its name are protocol, the same way the JSON on the wire
is: a server binds in here and a caller looks in here, and the two have
to agree or nothing finds anything. So it sits with the wire, in the
library both sides import, and `pymux/pipes/posix.py` binds in the room
this names.
"""

import getpass
import glob
import logging
import os
import stat
import tempfile
from typing import List

__all__ = [
    "socket_directory",
    "socket_paths",
]

logger = logging.getLogger(__name__)


def socket_directory() -> str:
    """
    The directory that holds this user's unnamed sockets.

    tmux keeps its sockets in `<base>/tmux-<uid>`: a directory it
    creates mode 0700 and then verifies before it uses -- owned by the
    user, closed to everyone else, a real directory and not a symlink.
    A socket named in a flat /tmp is a socket any account can squat in
    front of the bind, and then an attach lands on the squatter.
    Lillecarl/pymux#405.

    The bases, in order: `$PYMUX_TMPDIR`, the override tmux spells
    `$TMUX_TMPDIR`; then the per-user runtime directory the platform
    prefers, which is platformdirs' answer -- it reads
    `$XDG_RUNTIME_DIR` itself where that is the convention, and knows
    `/run/user/<uid>` for a login that never set it, the runtime
    directories of the BSDs, and the room macOS and Windows prefer
    (Lillecarl/pymux#421). No app name is asked for, so the room keeps
    the name `pymux-<uid>` and this move changed no path a running
    server had bound; then what Python names the temp directory, which
    on macOS is the user's own `$TMPDIR`.

    A room that fails the check is a refusal, never a repair: a
    directory somebody else built is exactly the one not to use. The
    refusal falls through to the next base -- a squat must not stop a
    server that a good base would hold -- and the last base's reason
    is what comes out when no base holds a good room.
    """
    bases = []
    value = os.environ.get("PYMUX_TMPDIR")
    # A relative base means another thing after `daemonize` moves
    # the process to /. Lillecarl/pymux#322.
    if value and os.path.isabs(value):
        bases.append(value)
    # Not at the top of the file: a detached command pays the import
    # of nothing it does not use. Lillecarl/pymux#392.
    from platformdirs import PlatformDirs

    # No app name: platformdirs appends one to the base when it is
    # given, and the room below is the per-user folder of this app
    # already -- `pymux-<uid>`, the name tmux spells `tmux-<uid>`.
    bases.append(PlatformDirs().user_runtime_dir)
    bases.append(tempfile.gettempdir())

    failure = None
    for base in bases:
        directory = os.path.join(base, "pymux-%d" % os.getuid())
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        try:
            _verify_the_socket_room(directory)
        except OSError as gone:
            # A room somebody else built is exactly the one not to use,
            # and a squat must not stop a server from starting while a
            # good base stands behind this one.
            logger.warning("Not using %s: %s", directory, gone)
            failure = gone
            continue
        return directory

    if failure is not None:
        # Every room failed its check. The last one is the temp
        # directory, the place the person can look at.
        raise failure
    raise OSError("no base can hold a pymux socket directory")


def _verify_the_socket_room(directory: str) -> None:
    """
    tmux's check, in tmux's order (`tmux.c` `make_label`): a real
    directory -- `lstat`, so a symlink does not pass -- owned by this
    user, with no permission for anybody else.
    """
    room = os.lstat(directory)
    if not stat.S_ISDIR(room.st_mode):
        raise OSError("%s is not a directory" % directory)
    if room.st_uid != os.getuid() or room.st_mode & 0o007:
        raise OSError("directory %s has unsafe permissions" % directory)


def socket_paths() -> List[str]:
    """
    Every pymux socket of this user that the default place holds.

    A server started with a socket path of its own is not in here. Name
    that path to reach it.

    The servers live in the per-UID room the server binds in, and one
    release also in the flat place they bound before it.
    Lillecarl/pymux#405.
    """
    user = getpass.getuser()
    found = glob.glob("%s/pymux.sock.%s.*" % (socket_directory(), user))
    found += glob.glob("%s/pymux.sock.%s.*" % (tempfile.gettempdir(), user))
    return sorted(set(path for path in found if _is_socket(path)))


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False
