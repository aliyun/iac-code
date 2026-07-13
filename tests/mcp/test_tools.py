from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from iac_code.mcp.tools import (
    ListMCPResourcesTool,
    MCPAuthenticateTool,
    MCPProgressEvent,
    MCPTool,
    ReadMCPResourceTool,
)
from iac_code.mcp.types import MCPResourceRecord, MCPToolRecord
from iac_code.tools.base import ToolContext


class FakeToolAnnotations:
    def model_dump(self, **kwargs: Any) -> dict[str, bool]:
        if kwargs.get("by_alias"):
            return {"readOnlyHint": True, "destructiveHint": False}
        return {"read_only_hint": True, "destructive_hint": False}


def test_mcp_tool_uses_discovered_schema_and_annotations() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="ros",
            tool_name="plan",
            public_name="mcp__ros__plan",
            description="Plan resources",
            input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            annotations={"readOnlyHint": True, "destructiveHint": False},
        ),
        session_id="session-1",
    )

    assert tool.name == "mcp__ros__plan"
    assert tool.description == "Plan resources"
    assert tool.input_schema["properties"]["name"]["type"] == "string"
    assert tool.is_read_only({}) is True
    assert tool.is_concurrency_safe({}) is True
    assert tool.is_destructive({}) is False
    assert tool.needs_event_queue() is True
    assert tool.user_facing_name({}) == "MCP ros:plan"
    assert tool.validate_input({"name": "vpc"}) == (True, "")
    valid, error = tool.validate_input({})
    assert valid is False
    assert "'name' is a required property" in error


@pytest.mark.asyncio
async def test_mcp_tool_accepts_sdk_annotation_objects() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="fs",
            tool_name="read_text_file",
            public_name="mcp__fs__read_text_file",
            description="Read file",
            input_schema={"type": "object"},
            annotations=FakeToolAnnotations(),  # type: ignore[arg-type]
        ),
        session_id="session-1",
    )

    result = await tool.check_permissions({"path": "pyproject.toml"})

    assert tool.is_read_only({}) is True
    assert tool.is_destructive({}) is False
    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.operation == {
        "publicName": "mcp__fs__read_text_file",
        "originalServerName": "fs",
        "originalToolName": "read_text_file",
        "isReadOnly": True,
        "isDestructive": False,
    }


@pytest.mark.asyncio
async def test_mcp_tool_execute_forwards_call_metadata_progress_and_converts_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeManager()
    event_queue = __import__("asyncio").Queue()
    tool = MCPTool(
        manager=manager,
        record=MCPToolRecord(
            server_name="ros",
            tool_name="generate",
            public_name="mcp__ros__generate",
            original_server_name="ros",
            original_tool_name="generate",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": False, "destructiveHint": True},
        ),
        session_id="session-1",
    )

    result = await tool.execute(
        tool_input={"resource": "vpc"},
        context=ToolContext(tool_use_id="tool-use-1", event_queue=event_queue),
    )

    assert manager.called_with["server_name"] == "ros"
    assert manager.called_with["tool_name"] == "generate"
    assert manager.called_with["arguments"] == {"resource": "vpc"}
    assert manager.called_with["meta"] == {
        "iac_code/toolUseId": "tool-use-1",
        "iac_code/mcp": {
            "publicName": "mcp__ros__generate",
            "originalServerName": "ros",
            "originalToolName": "generate",
        },
    }
    assert result.metadata is not None
    assert result.metadata["mcp"]["public_name"] == "mcp__ros__generate"
    assert result.metadata["mcp"]["original_server_name"] == "ros"
    assert result.metadata["mcp"]["original_tool_name"] == "generate"
    assert result.is_error is False
    assert "generated" in result.content
    assert '"id": "vpc-1"' in result.content

    event = event_queue.get_nowait()
    assert isinstance(event, MCPProgressEvent)
    assert event.server_name == "ros"
    assert event.tool_name == "generate"
    assert event.public_name == "mcp__ros__generate"
    assert event.tool_use_id == "tool-use-1"
    assert event.message == "halfway"


@pytest.mark.asyncio
async def test_mcp_tool_original_name_mapping_uses_public_and_original_names() -> None:
    manager = FakeManager()
    tool = MCPTool(
        manager=manager,
        record=MCPToolRecord(
            server_name="yuque",
            tool_name="search-docs",
            public_name="mcp__yuque__search_docs",
            original_server_name="yuque",
            original_tool_name="search-docs",
            input_schema={"type": "object"},
        ),
        session_id="session-1",
    )

    result = await tool.execute(tool_input={"query": "ros"}, context=ToolContext())

    assert tool.name == "mcp__yuque__search_docs"
    assert manager.called_with["server_name"] == "yuque"
    assert manager.called_with["tool_name"] == "search-docs"
    assert manager.called_with["meta"]["iac_code/mcp"] == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
    }
    assert result.metadata is not None
    assert result.metadata["mcp"]["public_name"] == "mcp__yuque__search_docs"
    assert result.metadata["mcp"]["original_server_name"] == "yuque"
    assert result.metadata["mcp"]["original_tool_name"] == "search-docs"


@pytest.mark.asyncio
async def test_mcp_tool_permission_audit_preserves_original_name_mapping_for_write_tool() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="yuque",
            tool_name="search-docs",
            public_name="mcp__yuque__search_docs",
            original_server_name="yuque",
            original_tool_name="search-docs",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": False, "destructiveHint": True},
        ),
        session_id="session-1",
    )

    result = await tool.check_permissions({"query": "ros"})

    assert result.behavior == "ask"
    assert result.message == "Allow MCP yuque:search-docs?"
    assert result.suggestions is not None
    assert [suggestion.tool_name for suggestion in result.suggestions] == ["mcp__yuque__search_docs"]
    assert [suggestion.rule_content for suggestion in result.suggestions] == [""]
    assert [suggestion.display_label() for suggestion in result.suggestions] == ["MCP yuque:search-docs"]
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.source == "permission_pipeline"
    assert result.audit.is_read_only is False
    assert result.audit.operation == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
        "isReadOnly": False,
        "isDestructive": True,
    }


@pytest.mark.asyncio
async def test_mcp_tool_permission_audit_preserves_original_name_mapping_for_read_only_tool() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="yuque",
            tool_name="search-docs",
            public_name="mcp__yuque__search_docs",
            original_server_name="yuque",
            original_tool_name="search-docs",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True, "destructiveHint": False},
        ),
        session_id="session-1",
    )

    result = await tool.check_permissions({"query": "ros"})

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "read_only"
    assert result.audit.source == "permission_pipeline"
    assert result.audit.is_read_only is True
    assert result.audit.operation == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
        "isReadOnly": True,
        "isDestructive": False,
    }


@pytest.mark.asyncio
async def test_mcp_tool_conflicting_read_only_and_destructive_annotations_asks_permission() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="yuque",
            tool_name="delete-docs",
            public_name="mcp__yuque__delete_docs",
            original_server_name="yuque",
            original_tool_name="delete-docs",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": True, "destructiveHint": True},
        ),
        session_id="session-1",
    )

    result = await tool.check_permissions({"query": "ros"})

    assert tool.is_read_only({}) is False
    assert tool.is_destructive({}) is True
    assert result.behavior == "ask"
    assert result.message == "Allow MCP yuque:delete-docs?"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.is_read_only is False
    assert result.audit.operation == {
        "publicName": "mcp__yuque__delete_docs",
        "originalServerName": "yuque",
        "originalToolName": "delete-docs",
        "isReadOnly": False,
        "isDestructive": True,
    }


@pytest.mark.asyncio
async def test_mcp_tool_permission_audit_omits_destructive_flag_when_annotation_absent() -> None:
    tool = MCPTool(
        manager=FakeManager(),
        record=MCPToolRecord(
            server_name="yuque",
            tool_name="search-docs",
            public_name="mcp__yuque__search_docs",
            original_server_name="yuque",
            original_tool_name="search-docs",
            input_schema={"type": "object"},
            annotations={"readOnlyHint": False},
        ),
        session_id="session-1",
    )

    result = await tool.check_permissions({"query": "ros"})

    assert result.audit is not None
    assert result.audit.operation == {
        "publicName": "mcp__yuque__search_docs",
        "originalServerName": "yuque",
        "originalToolName": "search-docs",
        "isReadOnly": False,
    }


@pytest.mark.asyncio
async def test_resource_tools_list_and_read_resources(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    manager = FakeManager()

    listed = await ListMCPResourcesTool(manager=manager).execute(tool_input={}, context=ToolContext())
    assert "skill://ros/vpc" in listed.content
    assert "text/markdown" in listed.content

    filtered = await ListMCPResourcesTool(manager=manager).execute(
        tool_input={"server": "other"},
        context=ToolContext(),
    )
    assert filtered.content == "No MCP resources are currently available."

    assert ReadMCPResourceTool.input_schema["required"] == ["server", "uri"]
    read = await ReadMCPResourceTool(manager=manager, session_id="session-1").execute(
        tool_input={"server": "ros", "uri": "skill://ros/vpc"},
        context=ToolContext(),
    )
    assert "Resource from MCP server 'ros'" in read.content
    assert "Use a VPC" in read.content


@pytest.mark.asyncio
async def test_authenticate_tool_returns_authorization_url() -> None:
    tool = MCPAuthenticateTool(
        server_name="remote", auth_url_factory=lambda server_name: "https://auth.example/authorize"
    )

    result = await tool.execute(tool_input={}, context=ToolContext())

    assert tool.name == "mcp__remote__authenticate"
    assert result.is_error is False
    assert "https://auth.example/authorize" in result.content


@pytest.mark.asyncio
async def test_authenticate_tool_runs_configured_auth_flow() -> None:
    called: list[str] = []

    async def auth_flow(server_name: str) -> str:
        called.append(server_name)
        return "authenticated {}".format(server_name)

    tool = MCPAuthenticateTool(server_name="remote", auth_flow=auth_flow)

    result = await tool.execute(tool_input={}, context=ToolContext())

    assert called == ["remote"]
    assert result.is_error is False
    assert result.content == "authenticated remote"


class FakeManager:
    def __init__(self) -> None:
        self.called_with: dict[str, Any] = {}

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        progress_callback=None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.called_with = {
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "meta": meta,
        }
        if progress_callback is not None:
            maybe_awaitable = progress_callback({"progress": 0.5, "total": 1.0, "message": "halfway"})
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return {
            "content": [{"type": "text", "text": "generated"}],
            "structuredContent": {"id": "vpc-1"},
        }

    def list_resources(self) -> list[MCPResourceRecord]:
        return [
            MCPResourceRecord(
                server_name="ros",
                uri="skill://ros/vpc",
                name="vpc",
                mime_type="text/markdown",
            )
        ]

    async def read_resource(self, uri: str, server_name: str | None = None) -> tuple[str, dict[str, Any]]:
        return (
            "ros",
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "text/markdown",
                        "text": "Use a VPC",
                    }
                ]
            },
        )
