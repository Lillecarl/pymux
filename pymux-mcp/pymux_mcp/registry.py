"""
A table of operations an agent can ask for, by name, with each one's
arguments as a model.

An operation is a typed async function whose first parameter is the
`libpymux.Server` it acts on. `@table.operation("pane.capture")` reads
the rest of its signature into a pydantic model, once. That one model
checks what an agent sends, is the JSON Schema `pymux_describe` answers,
and is the request body of the OpenAPI document (`openapi.py`). So the
signature is the only place an argument is written down.

**The table is an object that `api.OPERATIONS` holds, not a global
here.** A module-level table filled by importing `api` for its side
effect is empty under lazy imports, which never run an import nothing
reads; a read of `api.OPERATIONS` always runs `api`.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pydantic
from libpymux import Server

__all__ = ["Operation", "Registry", "UnknownOperation"]

#: An operation's answer is JSON of a shape each operation picks.
type Handler = Callable[..., Awaitable[Any]]


class UnknownOperation(LookupError):
    "No operation has that name."


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    handler: Handler
    arguments: type[pydantic.BaseModel]

    @property
    def description(self) -> str:
        return inspect.getdoc(self.handler) or ""

    @property
    def summary(self) -> str:
        "The first paragraph of the description, on one line: what `pymux_describe` lists."
        return " ".join(self.description.partition("\n\n")[0].split())

    def schema(self) -> dict[str, Any]:
        "The JSON Schema of the arguments."
        return self.arguments.model_json_schema()


class Registry(dict[str, Operation]):
    "The operations, by name."

    def operation(self, name: str) -> Callable[[Handler], Handler]:
        "Register an operation under `name`, with its arguments read from its signature."

        def register(handler: Handler) -> Handler:
            if name in self:
                raise ValueError("two operations named %s" % (name,))
            hints = typing.get_type_hints(handler, include_extras=True)
            _, *parameters = inspect.signature(handler).parameters.values()
            fields: dict[str, Any] = {
                parameter.name: (
                    hints[parameter.name],
                    ... if parameter.default is inspect.Parameter.empty else parameter.default,
                )
                for parameter in parameters
            }
            # An argument nobody declared is an agent's mistake, and saying
            # so beats running without what it meant.
            model = pydantic.create_model(
                name.replace(".", "_"),
                __config__=pydantic.ConfigDict(extra="forbid"),
                **fields,
            )
            self[name] = Operation(name, handler, model)
            return handler

        return register

    def find(self, name: str) -> Operation:
        found = self.get(name)
        if found is None:
            raise UnknownOperation("no operation %r; pymux_describe lists them" % (name,))
        return found

    async def call(self, server: Server, name: str, arguments: Mapping[str, Any]) -> Any:
        """
        Run the operation `name` on `server` with `arguments`.

        Raises `UnknownOperation` for a name nothing registered, and
        `pydantic.ValidationError` for arguments its model refuses.
        """
        found = self.find(name)
        parsed = found.arguments.model_validate(dict(arguments))
        return await found.handler(server, **{field: getattr(parsed, field) for field in type(parsed).model_fields})
