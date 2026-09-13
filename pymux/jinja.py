"""
The template language a person writes.

pymux's own format language is the `#X` symbols and `#{name}`, and that
is all of it: the regex is `#\\{([a-zA-Z0-9_]+)\\}`, so there is no
condition, no comparison, no padding and no arithmetic. tmux has all of
those, in `format.c`, which is seven thousand lines and two hundred
entries. **The choice was to write that, or to take a language that
exists.** Lillecarl/pymux#333.

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

from functools import lru_cache
from typing import TYPE_CHECKING, Callable, Dict, NamedTuple, Union

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
#: test the same way a `%H:%M` status line does.
EXTRA_FACTS: Dict[str, Callable[["FormatContext"], object]] = {
    "now": lambda context: context.pymux.displayed_now(),
}


class _Compiled(NamedTuple):
    "A template, and the names it asks for."

    template: Template
    names: frozenset


#: A sandbox, because a program in a pane can send commands, and
#: `display-message` takes a format. It can already send `run-shell`,
#: so this closes nothing that is open -- it is here so that a later
#: lock on the command socket does not have to find this first.
_environment = SandboxedEnvironment(undefined=ChainableUndefined)


@lru_cache(maxsize=TEMPLATES_TO_KEEP)
def _compile(source: str) -> Union[_Compiled, TemplateSyntaxError]:
    "The compiled template, or the reason there is none."
    try:
        return _Compiled(
            _environment.from_string(source),
            frozenset(find_undeclared_variables(_environment.parse(source))),
        )
    except TemplateSyntaxError as error:
        # Kept rather than raised, so that a template which cannot
        # parse is compiled once and not once a frame -- and logged
        # once with it.
        logger.warning("A template does not parse: %s", error)
        return error


def render(context: "FormatContext", source: str) -> str:
    "Draw this template with the facts of this context."
    from .format import tmux_variables

    compiled = _compile(source)
    if isinstance(compiled, TemplateSyntaxError):
        return BROKEN

    facts = {}
    for name in compiled.names:
        extra = EXTRA_FACTS.get(name)
        if extra is not None:
            facts[name] = extra(context)
            continue

        handler = tmux_variables.get(name)
        if handler is None:
            # Not a fact pymux has. jinja2 answers with nothing, the
            # same as `#{not_a_variable}` does.
            continue
        try:
            facts[name] = handler(context)
        except Exception:
            facts[name] = ""

    try:
        return compiled.template.render(facts)
    except Exception:
        logger.exception("A template failed while it was drawn.")
        return BROKEN
