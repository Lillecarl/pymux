"""
pymux-mcp without a server: the registry, the schemas, the two tools.

`check_pymux_mcp` in `drive_with_pty.py` runs the operations against a
server that is really there.
"""

from __future__ import annotations

import json

import pydantic
import pytest
from libpymux import Server
from mcp.shared.memory import create_connected_server_and_client_session
from pymux_mcp.api import OPERATIONS
from pymux_mcp.openapi import document
from pymux_mcp.registry import UnknownOperation
from pymux_mcp.server import build, render


async def _nowhere(tmp_path):
    return Server(str(tmp_path / "nothing.sock"))


def test_every_operation_has_a_summary_and_a_schema():
    assert {"tree", "sql", "command", "pane.capture", "pane.send", "pane.wait", "job.run", "job.tail"} <= set(
        OPERATIONS
    )
    for name, found in OPERATIONS.items():
        assert found.summary, name
        schema = found.schema()
        assert schema["type"] == "object", name
        assert schema["additionalProperties"] is False, name
        # The server is the operation's own first argument, and never the agent's.
        assert "server" not in schema.get("properties", {}), name


def test_a_summary_is_the_whole_first_paragraph():
    summary = OPERATIONS["job.tail"].summary
    assert "\n" not in summary
    assert summary.endswith("passes.")


async def test_an_unknown_operation_says_where_the_list_is(tmp_path):
    with pytest.raises(UnknownOperation, match="pymux_describe"):
        await OPERATIONS.call(await _nowhere(tmp_path), "pane.nothing", {})


async def test_an_argument_nobody_declared_is_refused(tmp_path):
    with pytest.raises(pydantic.ValidationError):
        await OPERATIONS.call(await _nowhere(tmp_path), "pane.capture", {"pane": "%1", "colour": "red"})


async def test_a_wait_of_no_time_is_refused_before_the_server_is_asked(tmp_path):
    "pymux refuses one, so the schema says so and an agent reads it there."
    with pytest.raises(pydantic.ValidationError):
        await OPERATIONS.call(await _nowhere(tmp_path), "pane.wait", {"pane": "%1", "timeout": 0})


async def test_a_missing_argument_is_refused_before_the_server_is_asked(tmp_path):
    with pytest.raises(pydantic.ValidationError):
        await OPERATIONS.call(await _nowhere(tmp_path), "pane.wait", {"pane": "%1"})


def test_the_openapi_document_has_one_path_per_operation():
    found = document("0.1")
    assert found["openapi"] == "3.1.0"
    assert set(found["paths"]) == {"/" + name for name in OPERATIONS}
    schemas = found["components"]["schemas"]
    # Every reference resolves inside the document.
    text = json.dumps(found)
    for name in {part.split('"')[0] for part in text.split("#/components/schemas/")[1:]}:
        assert name in schemas, name


def test_text_is_answered_as_it_is_and_the_rest_as_json():
    assert render("two\nlines\n") == "two\nlines\n"
    assert render(None) == "ok"
    assert json.loads(render({"a": [1, None]})) == {"a": [1, None]}


async def test_two_tools_over_mcp(tmp_path):
    async def server_for():
        return await _nowhere(tmp_path)

    async with create_connected_server_and_client_session(build(server_for)) as client:
        tools = await client.list_tools()
        assert sorted(tool.name for tool in tools.tools) == ["pymux", "pymux_describe"]

        listing = await client.call_tool("pymux_describe", {})
        assert not listing.isError
        assert "pane.capture: " in listing.content[0].text

        one = await client.call_tool("pymux_describe", {"op": "pane.wait"})
        assert json.loads(one.content[0].text)["arguments"]["required"] == ["pane", "timeout"]

        refused = await client.call_tool("pymux", {"op": "pane.wait", "args": {"pane": "%1"}})
        assert refused.isError
        assert "timeout" in refused.content[0].text

        gone = await client.call_tool("pymux", {"op": "tree"})
        assert gone.isError
        assert "no pymux server" in gone.content[0].text
