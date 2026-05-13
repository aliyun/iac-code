"""WebFetchTool - fetches web page content."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult


def _extract_text_from_html(html: str) -> str:
    """Extract plain text from HTML by removing tags and decoding entities.

    Steps:
    1. Remove <script>...</script> blocks (including content).
    2. Remove <style>...</style> blocks (including content).
    3. Strip all remaining HTML tags.
    4. Decode HTML entities (e.g. &amp; -> &).
    5. Collapse whitespace runs to a single space.

    Args:
        html: Raw HTML string.

    Returns:
        Plain text extracted from the HTML.
    """
    if not html:
        return ""

    # Remove script tags and their content
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Remove style tags and their content
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode HTML entities
    text = html_lib.unescape(text)

    # Collapse whitespace (spaces, tabs, newlines) to single space
    text = re.sub(r"[ \t]+", " ", text)

    # Collapse multiple blank lines to a single newline
    text = re.sub(r"\n\s*\n+", "\n", text)

    return text.strip()


class WebFetchTool(Tool):
    """Tool for fetching web page content via HTTP/HTTPS."""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch the content of a web page. Supports HTTP and HTTPS URLs. "
            "For HTML pages, the content is extracted as plain text (scripts and styles removed). "
            "Returns the page content truncated to max_length characters."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the web page to fetch. Must include scheme (http:// or https://).",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum number of characters to return. Defaults to 50000.",
                },
            },
            "required": ["url"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        url: str = tool_input.get("url", "")
        max_length: int = int(tool_input.get("max_length", 50000))

        # Validate URL is not empty
        if not url or not url.strip():
            return ToolResult.error(_("URL cannot be empty."))

        # Validate URL has scheme and netloc
        parsed = urlparse(url)
        if not parsed.scheme:
            return ToolResult.error(
                _("Invalid URL: missing scheme (e.g. http:// or https://). Got: {url}").format(url=url)
            )
        if not parsed.netloc:
            return ToolResult.error(_("Invalid URL: missing host/netloc. Got: {url}").format(url=url))

        headers = {"User-Agent": ("Mozilla/5.0 (compatible; iac-code/1.0; +https://github.com/ros-group/iac-code)")}

        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                text = response.text

                if "text/html" in content_type:
                    text = _extract_text_from_html(text)

                # Truncate to max_length
                if len(text) > max_length:
                    text = text[:max_length]

                return ToolResult.success(text)

        except httpx.HTTPStatusError as e:
            return ToolResult.error(_("HTTP error {status}: {url}").format(status=e.response.status_code, url=url))
        except httpx.HTTPError as e:
            return ToolResult.error(_("Failed to fetch {url}: {error}").format(url=url, error=str(e)))
        except Exception as e:
            return ToolResult.error(_("Unexpected error fetching {url}: {error}").format(url=url, error=str(e)))

    # UI rendering methods
    def render_tool_use_message(self, input: dict, *, verbose: bool = False):
        url = input.get("url", "")
        if not url:
            return None
        return url

    def render_tool_result_message(self, output: str, *, is_error: bool = False, verbose: bool = False):
        if is_error:
            return output
        lines = output.strip().splitlines()
        char_count = len(output.strip())
        summary = _("Fetched {chars} chars, {lines} lines").format(chars=char_count, lines=len(lines))
        if verbose:
            preview = "\n".join(lines[:50])
            if len(lines) > 50:
                preview += f"\n... ({len(lines) - 50} more lines)"
            return f"{summary}\n{preview}"
        return summary

    def render_tool_use_error_message(self, error: str):
        return error

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Fetch")

    def get_activity_description(self, input: dict | None = None) -> str:
        if input:
            url = input.get("url", "")
            short_url = url[:60] + "..." if len(url) > 60 else url
            return _("Fetching {url}").format(url=short_url)
        return _("Fetching web page...")

    def get_tool_use_summary(self, input: dict | None = None) -> str | None:
        if input:
            return input.get("url", "")[:80]
        return None

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    def is_destructive(self, input: dict | None = None) -> bool:
        return False
