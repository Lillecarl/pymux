from prompt_toolkit.filters import Filter

__all__ = [
    "HasPrefix",
    "WaitsForConfirmation",
    "InCommandMode",
    "WaitsForPrompt",
]


class HasPrefix(Filter):
    """
    When the prefix key (Usual C-b) has been pressed.
    """

    def __init__(self, pymux):
        self.pymux = pymux
        super().__init__()

    def __call__(self):
        try:
            return self.pymux.get_client_state().has_prefix
        except ValueError:
            return False


class WaitsForConfirmation(Filter):
    """
    Waiting for a yes/no key press.
    """

    def __init__(self, pymux):
        self.pymux = pymux
        super().__init__()

    def __call__(self):
        try:
            return bool(self.pymux.get_client_state().confirm_command)
        except ValueError:
            return False


class InCommandMode(Filter):
    """
    When ':' has been pressed.'
    """

    def __init__(self, pymux):
        self.pymux = pymux
        super().__init__()

    def __call__(self):
        try:
            client_state = self.pymux.get_client_state()
        except ValueError:
            return False
        return client_state.command_mode and not client_state.confirm_command


class WaitsForPrompt(Filter):
    """
    Waiting for input for a "command-prompt" command.
    """

    def __init__(self, pymux):
        self.pymux = pymux
        super().__init__()

    def __call__(self):
        try:
            client_state = self.pymux.get_client_state()
        except ValueError:
            return False
        return bool(client_state.prompt_command) and not client_state.confirm_command


def _confirm_or_prompt_or_command(pymux):
    "True when we are waiting for a command, prompt or confirmation."
    try:
        client_state = pymux.get_client_state()
    except ValueError:
        return False
    if (
        client_state.confirm_text
        or client_state.prompt_command
        or client_state.command_mode
    ):
        return True


# Three filters for copy mode stood here: `InScrollBuffer`,
# `InScrollBufferNotSearching` and `InScrollBufferSearching`. Each one
# read `pane.display_scroll_buffer`, and nothing had set that since
# copy mode moved into `ptterm`, so all three were always false and
# every key they guarded did nothing.
#
# The keys of copy mode live with the copy buffer now, in
# `ptterm.terminal.Terminal`. A filter here would have to ask that
# widget anyway. Lillecarl/pymux#133.
