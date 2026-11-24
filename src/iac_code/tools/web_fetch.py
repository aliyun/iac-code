"""WebFetchTool - fetches web page content."""

from __future__ import annotations

import html as html_lib
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from iac_code.a2a.backup import run_sync_fenced
from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_TABLE_CELLS = 10_000
TRUNCATION_MARKER = "\n\n[truncated]"
DEFAULT_MAX_LENGTH = 100_000


def _is_official_document(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "help.aliyun.com"
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith(("/zh/", "/en/", "/document_detail/"))
    )


def _table_span(cell: Tag, attribute: str) -> int:
    value = str(cell.get(attribute, "1")).strip()
    if not value.isascii() or not value.isdigit():
        raise ValueError("Invalid table span")
    value = value.lstrip("0") or "0"
    if len(value) > 4 or int(value) > 1000:
        raise ValueError("Table span exceeds extraction limits")
    return int(value)


class _WebMarkdownConverter(MarkdownConverter):
    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._remaining_table_cells = MAX_TABLE_CELLS

    def convert_td(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        # markdownify expands colspan even in tables that need no rowspan repair.
        width = max(1, _table_span(el, "colspan"))
        self._remaining_table_cells -= width
        if self._remaining_table_cells < 0:
            raise ValueError("Expanded table exceeds extraction limits")
        el["colspan"] = str(width)
        # A literal pipe must not create an extra Markdown table column.
        return getattr(super(), "convert_td")(el, text.replace("|", r"\|"), parent_tags)

    convert_th = convert_td


def _expand_table_rowspans(root: Tag, soup: BeautifulSoup) -> None:
    """Repeat merged row labels so Markdown cannot attach values to the wrong column."""
    remaining_cells = MAX_TABLE_CELLS
    remaining_chars = MAX_DOWNLOAD_BYTES
    tables = ([root] if root.name == "table" else []) + list(root.find_all("table"))
    for table in tables:
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        if all(
            cell.get("rowspan") in (None, "1") for row in rows for cell in row.find_all(["td", "th"], recursive=False)
        ):
            continue
        group_ends = [len(rows)] * len(rows)
        for index in range(len(rows) - 2, -1, -1):
            group_ends[index] = group_ends[index + 1] if rows[index].parent is rows[index + 1].parent else index + 1
        grid: dict[tuple[int, int], Tag | None] = {}
        for row_index, row in enumerate(rows):
            column = 0
            for cell in row.find_all(["td", "th"], recursive=False):
                while (row_index, column) in grid:
                    column += 1
                available_rows = group_ends[row_index] - row_index
                height = min(_table_span(cell, "rowspan") or available_rows, available_rows)
                width = max(1, _table_span(cell, "colspan"))
                if height > 1 and cell.find("table") is not None:
                    raise ValueError("Nested merged table cannot be expanded safely")
                remaining_cells -= height * width
                remaining_chars -= height * len(str(cell))
                if remaining_cells < 0 or remaining_chars < 0:
                    raise ValueError("Expanded table exceeds extraction limits")
                for y in range(height):
                    for x in range(width):
                        grid[row_index + y, column + x] = cell if x == 0 else None
                column += width
        columns = max((column + 1 for _, column in grid), default=0)
        # Rectangular padding creates cells outside the occupied span grid too.
        remaining_cells -= len(rows) * columns - len(grid)
        if remaining_cells < 0:
            raise ValueError("Expanded table exceeds extraction limits")
        for row_index, row in enumerate(rows):
            row.clear()
            for column in range(columns):
                original = grid.get((row_index, column))
                cell = deepcopy(original) if original is not None else soup.new_tag("td")
                cell.attrs.pop("rowspan", None)
                cell.attrs.pop("colspan", None)
                row.append(cell)


def _extract_markdown_from_html(html: str, url: str) -> str:
    """Convert locally; prefer the article body on Alibaba Cloud's documentation host."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup.find_all(["script", "style", "noscript", "template"]):
            element.decompose()
        root = soup.body or soup
        if urlparse(url).hostname == "help.aliyun.com":
            body = next(
                (
                    node
                    for selector in (".icms-help-docs-content", ".markdown-body", "#pc-markdown-container")
                    if (node := soup.select_one(selector)) is not None and node.get_text(strip=True)
                ),
                None,
            )
            if body is not None and body.get_text(strip=True):
                root = body
                title = soup.find("h1")
                if title is not None and root.find("h1") is None:
                    root.insert(0, deepcopy(title))
        for element in root.find_all(["a", "img"]):
            attribute = "href" if element.name == "a" else "src"
            value = element.get(attribute)
            if isinstance(value, str):
                target = urljoin(url, value.strip())
                if urlparse(target).scheme.lower() in {"http", "https", "mailto"}:
                    element[attribute] = target
                else:
                    element.attrs.pop(attribute, None)
        _expand_table_rowspans(root, soup)
        return _WebMarkdownConverter(heading_style="ATX", bullets="-", autolinks=False).convert_soup(root).strip()
    except Exception:
        # Keep existing read behavior if a malformed page cannot be converted.
        return _extract_text_from_html(html)


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


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class WebFetchTool(Tool):
    """Tool for fetching web page content via HTTP/HTTPS."""

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch the content of a web page. Supports HTTP and HTTPS URLs. "
            "HTML is converted to Markdown, preserving headings, tables, links and code blocks; "
            "Alibaba Cloud documentation returns the article body without site navigation. "
            "Official Alibaba Cloud help articles retain their full fetched body. Other websites "
            "are limited by max_length (default 100000). Large results are saved as tool-result:// "
            "files; use grep/read_file to search and read pages."
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
                    "description": "Maximum retained characters for non-official websites. Defaults to 100000.",
                },
            },
            "required": ["url"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        url: str = tool_input.get("url", "")
        max_length: int = int(tool_input.get("max_length", DEFAULT_MAX_LENGTH))

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
                downloaded = bytearray()
                download_truncated = False

                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_length = _parse_content_length(response.headers.get("content-length"))

                    byte_iter = response.aiter_bytes().__aiter__()
                    while True:
                        remaining_bytes = MAX_DOWNLOAD_BYTES - len(downloaded)
                        if remaining_bytes <= 0:
                            if content_length is not None:
                                download_truncated = content_length > MAX_DOWNLOAD_BYTES
                                break
                            try:
                                await byte_iter.__anext__()
                            except StopAsyncIteration:
                                break
                            download_truncated = True
                            break

                        try:
                            chunk = await byte_iter.__anext__()
                        except StopAsyncIteration:
                            break

                        if len(chunk) > remaining_bytes:
                            downloaded.extend(chunk[:remaining_bytes])
                            download_truncated = True
                            break

                        downloaded.extend(chunk)
                        if len(downloaded) >= MAX_DOWNLOAD_BYTES:
                            continue

                content_type = response.headers.get("content-type", "")
                text = bytes(downloaded).decode(response.encoding or "utf-8", errors="replace")

                if "text/html" in content_type.lower():
                    text = await run_sync_fenced(_extract_markdown_from_html, text, str(getattr(response, "url", url)))

                final_url = str(getattr(response, "url", url))
                official = _is_official_document(final_url)
                source_truncated = download_truncated or (not official and len(text) > max(0, max_length))
                if official:
                    if download_truncated:
                        text += TRUNCATION_MARKER
                elif max_length <= 0:
                    text = ""
                elif (download_truncated or len(text) > max_length) and max_length >= len(TRUNCATION_MARKER):
                    available_length = max_length - len(TRUNCATION_MARKER)
                    text = text[:available_length] + TRUNCATION_MARKER
                elif len(text) > max_length:
                    text = text[:max_length]

                return ToolResult(
                    content=text,
                    metadata={
                        "web_fetch": {
                            "url": final_url,
                            "official_document": official,
                            "source_truncated": source_truncated,
                        }
                    },
                )

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
                preview += "\n" + _("... ({count} more lines)").format(count=len(lines) - 50)
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
