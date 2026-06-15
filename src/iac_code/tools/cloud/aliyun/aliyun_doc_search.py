"""AliyunDocSearch - searches Alibaba Cloud documentation."""

from __future__ import annotations

from typing import Any

import httpx

from iac_code.i18n import _
from iac_code.services.telemetry import log_event
from iac_code.services.telemetry.names import Events
from iac_code.tools.base import Tool, ToolContext, ToolResult

_SEARCH_URL = "https://help.aliyun.com/help/json/search.json"
_TIMEOUT = 10
_PAGE_SIZE = 10


class AliyunDocSearch(Tool):
    """Tool for searching Alibaba Cloud documentation."""

    @property
    def name(self) -> str:
        return "aliyun_doc_search"

    @property
    def description(self) -> str:
        return (
            "Search Alibaba Cloud documentation. Returns document titles, summaries and links. "
            "Use category_id=28850 to limit results to ROS product docs."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "Search keywords",
                },
                "category_id": {
                    "type": "integer",
                    "description": "Product category ID, e.g. 28850 for ROS. Omit to search all products.",
                },
            },
            "required": ["keywords"],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def is_destructive(self, input: dict | None = None) -> bool:
        return False

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("DocSearch")

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return input.get("keywords")

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False) -> str | None:
        if is_error:
            return output
        if hasattr(self, "_last_summary"):
            return self._last_summary
        return self._summary_from_persisted_output(output)

    @staticmethod
    def _summary_from_persisted_output(output: str) -> str | None:
        for line in reversed(output.splitlines()):
            text = line.strip()
            if not text or "web_fetch" in text:
                continue
            lower = text.lower()
            if "found" in lower and "document" in lower:
                return text
            if "no documents found" in lower:
                return text
            if "文档" in text and ("找到" in text or "共" in text):
                return text
        return None

    def get_activity_description(self, input: dict | None = None) -> str | None:
        if input:
            keywords = input.get("keywords", "")
            return _("Searching docs for {keywords}...").format(keywords=keywords)
        return _("Searching docs...")

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        keywords = tool_input.get("keywords", "").strip()
        if not keywords:
            return ToolResult.error(_("keywords cannot be empty."))

        category_id = tool_input.get("category_id")

        params: dict[str, Any] = {
            "keywords": keywords,
            "topics": "DOCUMENT,PRODUCT",
            "language": "zh",
            "website": "cn",
            "pageSize": _PAGE_SIZE,
            "pageNum": 1,
        }
        if category_id is not None:
            params["categoryId"] = category_id

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(_SEARCH_URL, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            return ToolResult.error(_("HTTP error {status} when searching docs.").format(status=e.response.status_code))
        except httpx.HTTPError as e:
            return ToolResult.error(_("Failed to search docs: {error}").format(error=str(e)))

        try:
            data = response.json()
        except Exception:
            return ToolResult.error(_("Failed to parse search response as JSON."))

        if not data.get("success"):
            return ToolResult.error(_("Search API returned failure."))

        documents = data.get("data", {}).get("documents", {})
        items = documents.get("data", [])
        total = documents.get("totalCount", 0)

        # Emit doc search event
        category_str = str(category_id) if category_id is not None else None
        log_event(
            Events.DOC_SEARCHED,
            {
                "doc_source": "aliyun_ros_api",
                "search_category": category_str,
                "result_count": len(items),
                "outcome": "success" if items else "empty",
            },
        )

        if not items:
            self._last_summary = _("No documents found")
            return ToolResult.success(_("No documents found for keywords: {keywords}").format(keywords=keywords))

        lines: list[str] = []
        for i, item in enumerate(items, 1):
            title = item.get("title", "")
            content = item.get("content", "")
            url = item.get("url", "")
            lines.append(f"{i}. {title}")
            if content:
                lines.append(f"   {content}")
            if url:
                lines.append(f"   Link: {url}")
            lines.append("")

        count = len(items)
        self._last_summary = _("Found {count} documents (total {total})").format(count=count, total=total)
        lines.append(self._last_summary)
        lines.append(_("Use web_fetch tool to read full document content if needed."))

        return ToolResult.success("\n".join(lines))
