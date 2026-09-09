"""
Why an object that should be gone is still alive.

`weakref` says *that* something survived. This says *what holds it*,
which is the only form of that answer anybody can act on. A leak check
that reports "a Pane is still alive" sends the next person to read the
whole server; one that reports "held by `Arrangement.windows`" sends
them to one line.

**It walks referrers, not references.** `gc.get_referrers(obj)` answers
"what points at this", and the walk goes outward from the survivor
until it reaches something that is alive on purpose: a module, or a
frame of the program.

**A container is a step and not an answer.** A survivor is usually held
by a list, which is held by an instance's `__dict__`, which is held by
the instance. Only the instance is worth printing, so the walk jumps
over the plumbing: a `__dict__` becomes `Owner.attribute`, a closure
cell becomes the function that closes over it, and a bound method
becomes the object it is bound to.

**Three traps, and each cost a wrong answer once.**

- `gc.get_referrers` returns a fresh list, and that list is then a
  referrer of everything in it, so a walk that does not put it out of
  reach follows its own tail. The first version of this printed
  `list of 89 <- list of 3 <- list of 1 <- list of 1` for every
  survivor.
- The frames that are running hold the survivor while they ask about
  it. A chain that names one says "the code asking holds it".
- **A survivor is usually one of a ring**, because a widget, its
  control, its process and its screen all point at each other. The
  walk then goes round the ring and never names what holds the ring,
  which is the only thing worth knowing. So a caller that has several
  survivors passes them all as `ignore`, and the walk steps outside.

`python3Packages.objgraph` does this and more, with pictures. This is a
page of `gc` and it prints text a build log can hold, so the dependency
is not worth it yet. It is worth naming when somebody wants the
pictures.
"""

import gc
import inspect
import types

__all__ = [
    "what_holds",
    "why_it_is_alive",
]

#: How far the walk goes before it gives up. A chain longer than this
#: is not a chain a person reads.
DEPTH = 10


def _the_kind_of(obj) -> str:
    "What this object is, in as few words as read."
    if isinstance(obj, types.ModuleType):
        return "module %s" % (obj.__name__,)
    if isinstance(obj, types.FrameType):
        return "frame %s of %s" % (obj.f_code.co_name, obj.f_code.co_filename)
    if isinstance(obj, types.FunctionType):
        return "function %s" % (obj.__qualname__,)
    if isinstance(obj, types.MethodType):
        return "method %s" % (obj.__qualname__,)
    if isinstance(obj, type):
        return "class %s" % (obj.__qualname__,)
    if inspect.iscode(obj):
        return "code %s" % (obj.co_name,)
    if isinstance(obj, dict):
        return "dict of %d" % (len(obj),)
    if isinstance(obj, (list, tuple, set, frozenset)):
        return "%s of %d" % (type(obj).__qualname__, len(obj))

    return type(obj).__qualname__


def _the_key_in(holder, obj) -> str:
    "Under which key a dictionary keeps this object, in brackets."
    for key, value in list(holder.items()):
        if value is obj:
            return "[%r]" % (key,)
    for key in list(holder):
        if key is obj:
            return "[as a key]"
    return ""


def _owns_this_dict(mapping):
    """
    The object whose `__dict__` this is, if one is.

    An attribute of an instance shows up as "a dict holds it", and the
    dict says nothing. The owner and the attribute name say
    everything, and they are one step further out.
    """
    for holder in gc.get_referrers(mapping):
        if isinstance(holder, (types.ModuleType, type)):
            continue
        if getattr(holder, "__dict__", None) is mapping:
            return holder
    return None


def _closes_over(cell):
    "The functions that keep this closure cell."
    return [
        holder
        for holder in gc.get_referrers(cell)
        if isinstance(holder, types.FunctionType)
    ]


def _referrers_of(obj, seen: set) -> list:
    """
    What points at this object, without the plumbing of asking.

    The list `gc.get_referrers` builds is itself a referrer of
    everything in it, so it goes into `seen` before anything is
    followed. Without that the walk follows its own tail.
    """
    holders = gc.get_referrers(obj)
    seen.add(id(holders))

    return [holder for holder in holders if id(holder) not in seen]


def _the_frames_here() -> set:
    """
    Every frame that is running now, by id.

    The caller's frame holds the survivor while it asks about it, and
    so does every frame between. A chain that names one of them says
    "the code that is asking holds it", which is true and useless.
    """
    found = set()
    frame = inspect.currentframe()
    while frame is not None:
        found.add(id(frame))
        frame = frame.f_back
    return found


def _one_step(here, seen: set):
    """
    The next holder outward, and what to print for it.

    `None` when nothing that the collector can see holds this.
    """
    holders = _referrers_of(here, seen)
    if not holders:
        return None, None

    # An instance's attribute: name the instance and the attribute,
    # and step to the instance rather than to its dictionary.
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        owner = _owns_this_dict(holder)
        if owner is None:
            continue
        seen.add(id(holder))
        return owner, "%s%s" % (_the_kind_of(owner), _the_key_in(holder, here))

    # A closure cell: name the function that closes over it.
    for holder in holders:
        if type(holder).__name__ != "cell":
            continue
        closing = _closes_over(holder)
        if not closing:
            continue
        seen.add(id(holder))
        return closing[0], "%s (closes over it)" % (_the_kind_of(closing[0]),)

    # A bound method: name what it is bound to, unless that is where
    # the walk came from. A screen is held by a hundred bound methods
    # of its own -- one per sequence, in the parser's dispatch table --
    # and stepping to `__self__` there walks back onto the screen and
    # picks another of them. The step that says something is the one
    # to what holds the *method*.
    for holder in holders:
        if isinstance(holder, types.MethodType) and id(holder.__self__) not in seen:
            seen.add(id(holder))
            return holder.__self__, "%s (bound to it)" % (_the_kind_of(holder),)

    # Anything else: a plain container, an instance, a module. Prefer
    # something that is not a container, because a container is a step
    # and an instance is an answer.
    holders.sort(key=lambda holder: isinstance(holder, (dict, list, tuple, set)))
    holder = holders[0]

    label = _the_kind_of(holder)
    if isinstance(holder, dict):
        label += _the_key_in(holder, here)

    return holder, label


def what_holds(obj, ignore=()) -> list[str]:
    """
    The chain of holders of this object, outward, as text.

    `ignore` is objects the caller knows about and does not want in the
    answer: the list it collected the survivors into, and whatever it
    is holding them with.
    """
    chain = []
    seen = {id(obj)} | {id(one) for one in ignore} | _the_frames_here()
    here = obj

    for _step in range(DEPTH):
        holder, label = _one_step(here, seen)
        if holder is None:
            break

        chain.append(label)
        seen.add(id(holder))

        if isinstance(holder, (types.ModuleType, types.FrameType)):
            break

        here = holder

    return chain


def why_it_is_alive(name: str, obj, ignore=()) -> str:
    "One line per survivor, with the chain of holders after it."
    chain = what_holds(obj, ignore)
    if not chain:
        return "%s: alive, and nothing that gc can see holds it" % (name,)
    return "%s: held by %s" % (name, " <- ".join(chain))
