__all__ = [
    "create_client",
    "is_ssh_url",
    "list_clients",
]

#: What `-S` starts with when it names a machine rather than a path.
SCHEME = "ssh://"


def is_ssh_url(name: str | None) -> bool:
    "Whether this `-S` names a machine."
    return bool(name) and str(name).startswith(SCHEME)


def _windows() -> bool:
    # The import is here and not at the top of the module, so that
    # importing pymux.client costs nothing: every entry point imports
    # this package, and the toolkit is the attach path's to carry.
    # Lillecarl/pymux#392.
    from prompt_toolkit.utils import is_windows

    return is_windows()


def create_client(socket_name):
    # A machine, and not a path on this one. The import is here so that
    # a server never loads asyncssh: only a client that was given an
    # `ssh://` address needs it. Lillecarl/pymux#90.
    if is_ssh_url(socket_name):
        from .ssh import SshClient

        return SshClient(socket_name)

    if _windows():
        from .windows import WindowsClient

        return WindowsClient(socket_name)
    else:
        from .posix import PosixClient

        return PosixClient(socket_name)


def list_clients():
    if _windows():
        from .windows import list_clients

        return list_clients()
    else:
        from .posix import list_clients

        return list_clients()
