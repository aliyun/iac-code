"""Tests for AliyunDocSearch tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from iac_code.tools.base import ToolContext
from iac_code.tools.cloud.aliyun.aliyun_doc_search import AliyunDocSearch


@pytest.fixture
def tool() -> AliyunDocSearch:
    return AliyunDocSearch()


@pytest.fixture
def context() -> ToolContext:
    return ToolContext()


def _make_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response with the given JSON body."""
    response = httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("GET", "https://help.aliyun.com/help/json/search.json"),
    )
    return response


def _success_body(items: list[dict], total: int | None = None) -> dict:
    """Build a successful API response body."""
    return {
        "success": True,
        "data": {
            "documents": {
                "data": items,
                "totalCount": total if total is not None else len(items),
                "pageNum": 1,
                "pageSize": 10,
            }
        },
    }


class TestAliyunDocSearchProperties:
    def test_name(self, tool: AliyunDocSearch) -> None:
        assert tool.name == "aliyun_doc_search"

    def test_is_read_only(self, tool: AliyunDocSearch) -> None:
        assert tool.is_read_only() is True

    def test_is_not_destructive(self, tool: AliyunDocSearch) -> None:
        assert tool.is_destructive() is False

    def test_is_concurrency_safe(self, tool: AliyunDocSearch) -> None:
        assert tool.is_concurrency_safe({"keywords": "test"}) is True

    def test_input_schema_requires_keywords(self, tool: AliyunDocSearch) -> None:
        schema = tool.input_schema
        assert "keywords" in schema["required"]
        assert "category_id" not in schema["required"]

    def test_render_tool_use_message(self, tool: AliyunDocSearch) -> None:
        assert tool.render_tool_use_message({"keywords": "ECS"}) == "ECS"

    def test_get_activity_description(self, tool: AliyunDocSearch) -> None:
        desc = tool.get_activity_description({"keywords": "VPC"})
        assert "VPC" in desc


class TestAliyunDocSearchExecute:
    @pytest.mark.asyncio
    async def test_empty_keywords_returns_error(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        result = await tool.execute(tool_input={"keywords": "  "}, context=context)
        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_successful_search(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        items = [
            {"title": "ROS 概述", "content": "资源编排服务简介", "url": "https://help.aliyun.com/doc1"},
            {"title": "ROS 模板", "content": "模板语法说明", "url": "https://help.aliyun.com/doc2"},
        ]
        response = _make_response(_success_body(items, total=50))

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(tool_input={"keywords": "ROS"}, context=context)

        assert result.is_error is False
        assert "ROS 概述" in result.content
        assert "ROS 模板" in result.content
        assert "https://help.aliyun.com/doc1" in result.content

    @pytest.mark.asyncio
    async def test_no_results(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        response = _make_response(_success_body([], total=0))

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(tool_input={"keywords": "xyznonexistent"}, context=context)

        assert result.is_error is False
        assert "xyznonexistent" in result.content

    @pytest.mark.asyncio
    async def test_category_id_passed_as_param(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        response = _make_response(_success_body([{"title": "t", "content": "c", "url": "u"}]))

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await tool.execute(tool_input={"keywords": "ROS", "category_id": 28850}, context=context)

            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert params["categoryId"] == 28850

    @pytest.mark.asyncio
    async def test_category_id_not_sent_when_omitted(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        response = _make_response(_success_body([{"title": "t", "content": "c", "url": "u"}]))

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await tool.execute(tool_input={"keywords": "ECS"}, context=context)

            call_kwargs = mock_client.get.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
            assert "categoryId" not in params

    @pytest.mark.asyncio
    async def test_http_error(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        response = _make_response({"success": False}, status_code=500)
        response.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError("Server Error", request=response.request, response=response)
        )

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(tool_input={"keywords": "test"}, context=context)

        assert result.is_error is True
        assert "500" in result.content

    @pytest.mark.asyncio
    async def test_network_error(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(tool_input={"keywords": "test"}, context=context)

        assert result.is_error is True
        assert "Connection refused" in result.content

    @pytest.mark.asyncio
    async def test_api_failure_flag(self, tool: AliyunDocSearch, context: ToolContext) -> None:
        response = _make_response({"success": False})

        with patch("iac_code.tools.cloud.aliyun.aliyun_doc_search.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await tool.execute(tool_input={"keywords": "test"}, context=context)

        assert result.is_error is True
