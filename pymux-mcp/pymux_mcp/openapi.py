"""
The operations as an OpenAPI 3.1 document.

One `POST /<operation>` each, its body the operation's arguments. OpenAPI
3.1 is JSON Schema 2020-12, which is what pydantic writes, so the
schemas go in unchanged. They go into `components/schemas` and not one
by one: a schema that names another refers to it from the root of the
document, and only one place in the document can hold them all.
"""

from __future__ import annotations

from typing import Any

from pydantic.json_schema import models_json_schema

from .api import OPERATIONS

__all__ = ["document"]


def document(version: str) -> dict[str, Any]:
    operations = sorted(OPERATIONS.values(), key=lambda found: found.name)
    refs, definitions = models_json_schema(
        [(found.arguments, "validation") for found in operations],
        ref_template="#/components/schemas/{model}",
    )
    paths = {
        "/" + found.name: {
            "post": {
                "operationId": found.name,
                "summary": found.summary,
                "description": found.description,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": refs[found.arguments, "validation"]}},
                },
                "responses": {"200": {"description": "The operation's answer.", "content": {"application/json": {}}}},
            }
        }
        for found in operations
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "pymux", "version": version},
        "paths": paths,
        "components": {"schemas": definitions.get("$defs", {})},
    }
