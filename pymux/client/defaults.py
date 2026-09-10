from prompt_toolkit.utils import is_windows

__all__ = [
    "create_client",
    "list_clients",
]


def create_client(socket_name):
    # A machine, and not a path on this one. The import is here so that
    # a server never loads asyncssh: only a client that was given an
    # `ssh://` address needs it. Lillecarl/pymux#90.
    from .ssh import is_an_ssh_url

    if is_an_ssh_url(socket_name):
        from .ssh import SshClient

        return SshClient(socket_name)

    if is_windows():
        from .windows import WindowsClient

        return WindowsClient(socket_name)
    else:
        from .posix import PosixClient

        return PosixClient(socket_name)


def list_clients():
    if is_windows():
        from .windows import list_clients

        return list_clients()
    else:
        from .posix import list_clients

        return list_clients()
