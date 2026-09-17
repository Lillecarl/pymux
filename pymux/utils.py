"""
Some utilities.
"""

import os
import sys

__all__ = [
    "daemonize",
    "nonblocking",
    "get_default_shell",
    "keys_are_vi",
]


def keys_are_vi(environ=os.environ) -> bool:
    """
    Whether copy mode and the status line take vi keys.

    tmux reads the editor of the person and switches both, in
    `tmux.c`: "Override keys to vi if VISUAL or EDITOR are set". Any
    editor whose name holds "vi" gives vi keys and everything else
    gives emacs keys.

    **It is a substring of the name and not the start of it**, so
    `nvim` gives vi keys. That is the point of writing it that way.
    """
    editor = environ.get("VISUAL")
    if editor is None:
        editor = environ.get("EDITOR")
    if editor is None:
        return False
    return "vi" in os.path.basename(editor)


def daemonize(stdin="/dev/null", stdout="/dev/null", stderr="/dev/null"):
    """
    Double fork-trick. For starting a posix daemon.

    This forks the current process into a daemon. The stdin, stdout, and stderr
    arguments are file names that will be opened and be used to replace the
    standard file descriptors in sys.stdin, sys.stdout, and sys.stderr. These
    arguments are optional and default to /dev/null. Note that stderr is opened
    unbuffered, so if it shares a file with stdout then interleaved output may
    not appear in the order that you expect.

    Thanks to:
    http://code.activestate.com/recipes/66012-fork-a-daemon-process-on-unix/
    """
    # Do first fork.
    try:
        pid = os.fork()
        if pid > 0:
            os.waitpid(pid, 0)
            return 0  # Return 0 from first parent.
    except OSError as e:
        sys.stderr.write("fork #1 failed: (%d) %s\n" % (e.errno, e.strerror))
        sys.exit(1)

    # Decouple from parent environment.
    # The recipe sets `umask(0)` here, for a daemon that picks its own
    # later. This daemon is the server, and every pane forks from it,
    # so the umask it keeps is the caller's -- the one the person
    # chose for the shells that follow. Nothing between here and the
    # return needs a known umask. Lillecarl/pymux#398.
    os.chdir("/")
    os.setsid()

    # Do second fork.
    try:
        pid = os.fork()
        if pid > 0:
            # `os._exit` and not `sys.exit`. This process exists only to
            # orphan the one below it, and it holds a copy of everything
            # the caller had. `sys.exit` raises SystemExit, which runs a
            # full interpreter shutdown on that copy: the event loop the
            # caller built is finalised here, and its descriptors belong
            # to a process that is still using them.
            #
            # Python 3.14 prints what that raises, and stderr still
            # belongs to the caller at this point -- the redirection is
            # below. So `new-session` answered with a traceback on
            # stderr, and any tool that reads stderr called it a failed
            # command. libtmux does. Lillecarl/pymux#321.
            os._exit(0)
    except OSError as e:
        sys.stderr.write("fork #2 failed: (%d) %s\n" % (e.errno, e.strerror))
        sys.exit(1)

    # Now I am a daemon!

    # Redirect standard file descriptors.

    # NOTE: For debugging, you meight want to take these instead of /dev/null.
    # so = open('/tmp/log2', 'ab+')
    # se = open('/tmp/log2', 'ab+', 0)

    si = open(stdin, "rb")
    so = open(stdout, "ab+")
    se = open(stderr, "ab+", 0)
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())

    # Return 1 from daemon.
    return 1


class nonblocking:
    """
    Make fd non blocking.
    """

    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        import fcntl

        self.orig_fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.orig_fl | os.O_NONBLOCK)

    def __exit__(self, *args):
        import fcntl

        fcntl.fcntl(self.fd, fcntl.F_SETFL, self.orig_fl)


def get_default_shell():
    """
    return the path to the default shell for the current user.
    """
    # The import is here and not at the top of the module, so that
    # importing pymux.utils costs nothing: every entry point imports
    # this module, and only a Windows machine needs the toolkit to
    # say so. Lillecarl/pymux#392.
    from prompt_toolkit.utils import is_windows

    if is_windows():
        return "cmd.exe"
    else:
        import getpass
        import pwd

        if "SHELL" in os.environ:
            return os.environ["SHELL"]
        else:
            username = getpass.getuser()
            shell = pwd.getpwnam(username).pw_shell
            return shell
