"""
The template language a person writes.

pymux's own format language is the `#X` symbols and `#{name}`, and that
is all of it: the regex is `#\\{([a-zA-Z0-9_]+)\\}`, so there is no
condition, no comparison, no padding and no arithmetic. tmux has all of
those, in `format.c`, which is seven thousand lines and two hundred
entries. **The choice was to write that, or to take a language that
exists.** Lillecarl/pyterm#335.

So a string that holds `{{` or `{%` is rendered here instead, and a
string that does not is formatted the way it always was. A `.tmux.conf`
carried over keeps working, and `-F` keeps speaking tmux format for
libtmux and libpymux, which read it as a protocol.

**A template sees the same facts, under the same names.** Everything of
`format.tmux_variables` is a name here -- `{{ session_name }}` is
`#{session_name}` -- plus `now`, because whole-string `strftime` is a
trap that turns any `%` in a status line into a directive.

Two things hold the frame rate up, and both matter because a status
line is formatted several times a frame:

- a compiled template is kept, keyed by its source, and
- only the names the template actually uses are read.

`find_undeclared_variables` is what says which those are, so a template
that asks for one fact costs one fact and not forty.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple

from jinja2 import ChainableUndefined, Template
from jinja2.exceptions import TemplateSyntaxError
from jinja2.meta import find_undeclared_variables
from jinja2.sandbox import SandboxedEnvironment

from .log import logger

if TYPE_CHECKING:
    from pymux.format import FormatContext

__all__ = ["render"]

#: How many compiled templates to keep. A status line, a window status
#: format, a pane title and whatever a person types at the command
#: prompt: the number of distinct template strings a session uses is
#: small and does not grow while it runs.
TEMPLATES_TO_KEEP = 128

#: What a template that cannot be compiled draws. Silence would be
#: wrong here: the tmux path loses one variable when it cannot answer,
#: and a template that does not parse loses the whole line, so a person
#: has to be able to see which of the two happened.
BROKEN = "<template error>"

#: Facts that are not `#{name}` variables. `now` is the clock that
#: test-mode pins, so a template that prints the time holds still in a
#: test the same way a `%H:%M` status line does. `user_vars` is what the
#: pane's program set with "OSC 1337 ; SetUserVar", such as
#: `{{ user_vars.git_branch }}`. The tags of `set-tag` are dictionaries
#: here, where a tmux format spells them as one string.
EXTRA_FACTS: dict[str, Callable[[FormatContext], object]] = {
    "now": lambda context: context.pymux.displayed_now(),
    "user_vars": lambda context: dict(context.the_pane().user_vars),
    "session_tags": lambda context: dict(context.session.tags),
    "window_tags": lambda context: dict(context.the_window().tags),
    "pane_tags": lambda context: dict(context.the_pane().tags),
}


type _Reader = Callable[[FormatContext], object]


class _Compiled(NamedTuple):
    "A template, and how to read each fact it asks for and what it is when that fails."

    template: Template
    readers: tuple[tuple[str, _Reader, object], ...]
    #: jinja2's own globals (`range`, `namespace`, ...). `Template.render`
    #: copies them out of a `ChainMap` on every call, a name at a time,
    #: and that cost a keystroke more than every fact it read.
    #: Lillecarl/pyterm#962.
    globals: dict[str, object]


def _readers(names: frozenset[str]) -> tuple[tuple[str, _Reader, object], ...]:
    "Each name pymux has a fact for. jinja2 answers any other with nothing, as `#{not_a_variable}` does."
    from .format import tmux_variables

    readers: list[tuple[str, _Reader, object]] = []
    for name in sorted(names):
        extra = EXTRA_FACTS.get(name)
        if extra is not None:
            readers.append((name, extra, {}))
        elif (handler := tmux_variables.get(name)) is not None:
            readers.append((name, handler, ""))
    return tuple(readers)


#: A sandbox, because a program in a pane can send commands, and
#: `display-message` takes a format. It can already send `run-shell`,
#: so this closes nothing that is open -- it is here so that a later
#: lock on the command socket does not have to find this first.
_environment = SandboxedEnvironment(undefined=ChainableUndefined)


@lru_cache(maxsize=TEMPLATES_TO_KEEP)
def _compile(source: str) -> _Compiled | TemplateSyntaxError:
    "The compiled template, or the reason there is none."
    try:
        tree = _environment.parse(source)
        template = _environment.from_string(tree)
        return _Compiled(template, _readers(frozenset(find_undeclared_variables(tree))), dict(template.globals))
    except TemplateSyntaxError as error:
        # Kept rather than raised, so that a template which cannot
        # parse is compiled once and not once a frame -- and logged
        # once with it.
        logger.warning("A template does not parse: %s", error)
        return error


def render(context: FormatContext, source: str) -> str:
    "Draw this template with the facts of this context."
    compiled = _compile(source)
    if isinstance(compiled, TemplateSyntaxError):
        return BROKEN

    facts = compiled.globals.copy()
    for name, read, failed in compiled.readers:
        try:
            facts[name] = read(context)
        except Exception:
            facts[name] = failed

    template = compiled.template
    try:
        # `Template.render`, less its two walks of the globals: `facts`
        # holds them, and only `{% import %}` reads the set of their
        # names, which a template with no loader cannot do.
        drawn = _environment.context_class(_environment, facts, template.name, template.blocks)
        return "".join(template.root_render_func(drawn))
    except Exception:
        logger.exception("A template failed while it was drawn.")
        return BROKEN
