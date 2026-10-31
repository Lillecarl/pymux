"""
A person's own terminals: copy this to `$XDG_CONFIG_HOME/pymux/terminals.py`.

pymux knows a terminal by the first word of its XTVERSION answer, in
lower case and cut at a parenthesis: "MyTerm 2.1" is "myterm". It falls
back to TERM when the terminal does not answer. An entry here with the
name of a built-in one wins over it.

`pymux/connection/known_terminals.py` says what each field means.
"""

from pyte.osc import Osc

from pymux.connection.known_terminals import KnownTerminal

TERMINALS = (
    KnownTerminal(
        "myterm",
        # The TERM values that name this terminal and no other.
        terms=frozenset({"xterm-myterm"}),
        # The first version that draws kitty's Unicode placeholders.
        placeholders=(2, 0),
        # The notification OSC it shows.
        notification=Osc.SIMPLE_NOTIFICATION,
    ),
)
