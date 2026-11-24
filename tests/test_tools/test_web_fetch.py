"""Tests for the WebFetchTool."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import httpx
import pytest
from bs4 import BeautifulSoup

import iac_code.tools.web_fetch as web_fetch_module
from iac_code.tools.base import ToolContext
from iac_code.tools.web_fetch import (
    WebFetchTool,
    _expand_table_rowspans,
    _extract_markdown_from_html,
    _extract_text_from_html,
)


@pytest.fixture
def web_fetch_tool():
    """Create a WebFetchTool instance."""
    return WebFetchTool()


@pytest.fixture
def context():
    """Create a default ToolContext."""
    return ToolContext()


class AsyncByteStream:
    """Minimal async response stream for web_fetch tests."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        headers: dict[str, str] | None = None,
        encoding: str | None = "utf-8",
        status_error: httpx.HTTPStatusError | None = None,
    ):
        self.chunks = chunks
        self.headers = headers or {}
        self.encoding = encoding
        self.status_error = status_error
        self.consumed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


class AsyncClientStreamOnly:
    """AsyncClient stand-in that only exposes stream()."""

    def __init__(
        self,
        stream: AsyncByteStream | None = None,
        *,
        stream_error: httpx.HTTPError | None = None,
    ):
        self._stream = stream
        self._stream_error = stream_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str):
        if self._stream_error:
            raise self._stream_error
        return self._stream


class TestExtractTextFromHtml:
    """Tests for the _extract_text_from_html helper."""

    def test_strips_script_tags(self):
        html = "<html><body><script>alert('test');</script><p>Hello</p></body></html>"
        result = _extract_text_from_html(html)
        assert "alert" not in result
        assert "Hello" in result

    def test_strips_style_tags(self):
        html = "<html><head><style>body { color: red; }</style></head><body><p>World</p></body></html>"
        result = _extract_text_from_html(html)
        assert "color" not in result
        assert "World" in result

    def test_strips_html_tags(self):
        html = "<div><p>Clean <b>text</b> here</p></div>"
        result = _extract_text_from_html(html)
        assert "<" not in result
        assert ">" not in result
        assert "Clean" in result
        assert "text" in result
        assert "here" in result

    def test_decodes_html_entities(self):
        html = "<p>Hello &amp; World &lt;test&gt; &quot;quoted&quot;</p>"
        result = _extract_text_from_html(html)
        assert "&amp;" not in result
        assert "&lt;" not in result
        assert "&gt;" not in result
        assert "&" in result
        assert "<" in result
        assert ">" in result

    def test_collapses_whitespace(self):
        html = "<p>Hello   \t   World</p>"
        result = _extract_text_from_html(html)
        # Multiple spaces/tabs should be collapsed to single space
        assert "Hello World" in result

    def test_handles_empty_html(self):
        result = _extract_text_from_html("")
        assert result == "" or result.strip() == ""

    def test_strips_nested_script_in_body(self):
        html = "<body><p>Visible</p><script type='text/javascript'>var x = 1;</script></body>"
        result = _extract_text_from_html(html)
        assert "var x" not in result
        assert "Visible" in result


class TestHtmlToMarkdown:
    @pytest.mark.parametrize("tables,cells", [(1, 12), (2, 6)])
    def test_colspan_only_tables_share_a_budget_before_markdown_expansion(self, tables, cells):
        html = ("<table><tr>" + '<th colspan="1000">keep</th>' * cells + "</tr></table>") * tables
        output = _extract_markdown_from_html(html, "https://example.com/guide")
        assert output == _extract_text_from_html(html)
        assert "keep" in output and len(output) < 1000

    def test_small_colspan_only_table_retains_markdown_columns(self):
        output = _extract_markdown_from_html(
            '<table><tr><th colspan=" 2 ">Location</th><th>Spec</th></tr></table>',
            "https://example.com",
        )
        assert [cell.strip() for cell in output.splitlines()[0].split("|")[1:-1]] == ["Location", "", "Spec"]

    @pytest.mark.parametrize("span", ["2", " 2 ", "0"])
    def test_rowspan_variants_keep_region_zone_and_spec_in_their_columns(self, span):
        html = (
            "<table><thead><tr><th>Region</th><th>Zone</th><th>Spec</th></tr></thead>"
            f'<tbody><tr><td rowspan="{span}">Hangzhou</td><td>J</td><td>First</td></tr>'
            "<tr><td>K</td><td>Second</td></tr></tbody></table>"
        )
        output = _extract_markdown_from_html(html, "https://example.com")
        assert "| Hangzhou | J | First |" in output
        assert "| Hangzhou | K | Second |" in output
        assert "| K | Second |  |" not in output.splitlines()

    @pytest.mark.parametrize("span", ["0", "5"])
    def test_rowspan_stops_at_the_end_of_its_row_group(self, span):
        html = (
            f'<table><tbody><tr><td rowspan="{span}">Hangzhou</td><td>J</td></tr>'
            "<tr><td>K</td></tr></tbody><tbody><tr><td>Beijing</td><td>A</td></tr></tbody></table>"
        )
        output = _extract_markdown_from_html(html, "https://example.com")
        assert "| Hangzhou | K |" in output
        assert "| Beijing | A |" in output
        assert "| Hangzhou | Beijing |" not in output

    @pytest.mark.parametrize("span", ["-2", "two", "1.5"])
    def test_unreliable_rowspan_falls_back_instead_of_inventing_column_associations(self, span):
        html = f'<table><tr><td rowspan="{span}">Region</td><td>Zone</td></tr></table>'
        assert _extract_markdown_from_html(html, "https://example.com") == _extract_text_from_html(html)

    def test_nested_merged_table_falls_back_before_duplicating_its_subtree(self):
        html = '<table><tr><td rowspan="2"><table><tr><td>Nested</td></tr></table></td></tr><tr></tr></table>'
        assert _extract_markdown_from_html(html, "https://example.com") == _extract_text_from_html(html)

    def test_aliyun_table_selected_as_article_root_still_repairs_rowspans(self):
        html = (
            '<table class="markdown-body"><tr><td rowspan="2">Hangzhou</td><td>J</td></tr><tr><td>K</td></tr></table>'
        )
        assert "Hangzhou | K" in _extract_markdown_from_html(html, "https://help.aliyun.com/guide")

    @pytest.mark.parametrize("tables,rows,columns", [(1, 60, 200), (2, 60, 100)])
    def test_sparse_table_padding_is_counted_before_allocating_cells(self, tables, rows, columns):
        table = f'<table><tr><td rowspan="2" colspan="{columns}">keep</td></tr>'
        table += "<tr></tr>" * (rows - 1) + "</table>"
        html = table * tables
        soup = BeautifulSoup(html, "html.parser")
        with pytest.raises(ValueError, match="Expanded table exceeds extraction limits"):
            _expand_table_rowspans(soup, soup)
        assert len(soup.find_all(["td", "th"])) <= 10_000
        output = _extract_markdown_from_html(html, "https://example.com/guide")
        assert "keep" in output and "|" not in output

    def test_table_at_cell_budget_still_preserves_all_rows(self):
        html = '<table><tr><td rowspan="2" colspan="100">keep</td></tr>' + "<tr></tr>" * 99 + "</table>"
        soup = BeautifulSoup(html, "html.parser")
        _expand_table_rowspans(soup, soup)
        assert len(soup.find_all("td")) == 10_000
        assert len(soup.find_all("tr")) == 100

    def test_preserves_document_structure_and_resolves_links(self):
        html = """<h1>Guide</h1><h2>Steps</h2><ul><li>First</li><li>Second</li></ul>
        <p><a href="../api#params">API</a></p><pre><code>first\n  second</code></pre>
        <table><tr><th>Region</th><th>Spec</th></tr>
        <tr><td>Hangzhou J</td><td>ecs.r9i.xlarge</td></tr></table>"""

        output = _extract_markdown_from_html(html, "https://example.com/docs/guide")

        assert "# Guide" in output
        assert "## Steps" in output
        assert "- First\n- Second" in output
        assert "[API](https://example.com/api#params)" in output
        assert "```\nfirst\n  second\n```" in output
        assert "| Region | Spec |\n| --- | --- |\n| Hangzhou J | ecs.r9i.xlarge |" in output

    @pytest.mark.parametrize("selector", ["icms-help-docs-content", "markdown-body", "pc-markdown-container"])
    def test_aliyun_document_uses_body_and_preserves_outside_title(self, selector):
        attribute = f'id="{selector}"' if selector == "pc-markdown-container" else f'class="{selector}"'
        html = f"""<nav>Products and account</nav><header><h1>TDX guide</h1></header>
        <div {attribute}><h2>Supported regions</h2><p>Hangzhou J</p></div>
        <footer>Related products and legal links</footer>"""

        output = _extract_markdown_from_html(html, "https://help.aliyun.com/zh/ecs/guide")

        assert output.startswith("# TDX guide")
        assert "## Supported regions" in output
        assert "Hangzhou J" in output
        assert "Products and account" not in output
        assert "Related products" not in output

    def test_aliyun_body_prefers_inner_content_and_does_not_duplicate_heading(self):
        html = """<div id="pc-markdown-container">Copy button<div class="markdown-body">
        Breadcrumb<div class="icms-help-docs-content"><h1>Guide</h1><p>Body</p></div></div></div>"""

        output = _extract_markdown_from_html(html, "https://help.aliyun.com/guide")

        assert output.count("# Guide") == 1
        assert "Copy button" not in output
        assert "Breadcrumb" not in output

    @pytest.mark.parametrize("host", ["example.com", "help.aliyun.com.evil.example"])
    def test_aliyun_body_optimization_does_not_apply_to_other_hosts(self, host):
        html = '<p>Outside content</p><div class="markdown-body"><p>Inside content</p></div>'

        output = _extract_markdown_from_html(html, f"https://{host}/guide")

        assert "Outside content" in output
        assert "Inside content" in output

    def test_missing_or_empty_aliyun_body_preserves_page_content(self):
        html = '<p>Actual content</p><div class="markdown-body"></div>'

        assert "Actual content" in _extract_markdown_from_html(html, "https://help.aliyun.com/guide")

    def test_rowspan_and_colspan_keep_resource_columns_associated(self):
        html = """<table><tr><th colspan="2">Location</th><th>Spec</th></tr>
        <tr><td rowspan="2">Hangzhou</td><td>J</td><td>First</td></tr>
        <tr><td>K</td><td>Second</td></tr></table>"""

        output = _extract_markdown_from_html(html, "https://help.aliyun.com/guide")

        assert "| Location |  | Spec |" in output
        assert "| Hangzhou | J | First |" in output
        assert "| Hangzhou | K | Second |" in output

    def test_table_cell_pipe_does_not_create_extra_column(self):
        html = "<table><tr><th>Command</th><th>Meaning</th></tr><tr><td>a | b</td><td>Pipe</td></tr></table>"

        output = _extract_markdown_from_html(html, "https://example.com")

        assert r"| a \| b | Pipe |" in output

    def test_conversion_removes_active_content_and_unsafe_link_schemes(self):
        html = """<script>badScript()</script><style>badStyle</style>
        <p><a href="javascript:alert(1)">Useful label</a></p>
        <img src="data:text/html,badImage" alt="Diagram">
        <p><a href="//help.aliyun.com/guide">Safe link</a></p>"""

        output = _extract_markdown_from_html(html, "https://example.com")

        assert "Useful label" in output
        assert "badScript" not in output
        assert "badStyle" not in output
        assert "javascript:" not in output
        assert "data:text/html" not in output
        assert "[Safe link](https://help.aliyun.com/guide)" in output

    def test_extreme_heading_does_not_expand_output(self):
        output = _extract_markdown_from_html("<h9999999>Title</h9999999>", "https://example.com")

        assert "Title" in output
        assert len(output) < 100

    def test_extreme_table_spans_fall_back_without_expanding_output(self):
        html = '<table><tr><td rowspan="999999" colspan="999999">Value</td></tr></table>'

        output = _extract_markdown_from_html(html, "https://example.com")

        assert "Value" in output
        assert len(output) < 100

    def test_converter_failure_preserves_plain_text_fallback(self):
        with patch("iac_code.tools.web_fetch._WebMarkdownConverter.convert_soup", side_effect=ValueError("bad page")):
            output = _extract_markdown_from_html("<h1>Readable</h1><p>Body</p>", "https://example.com")

        assert "Readable" in output
        assert "Body" in output


class TestWebFetchToolProperties:
    """Tests for WebFetchTool properties."""

    def test_name(self, web_fetch_tool):
        assert web_fetch_tool.name == "web_fetch"

    def test_description(self, web_fetch_tool):
        assert isinstance(web_fetch_tool.description, str)
        assert len(web_fetch_tool.description) > 0

    def test_schema_has_url_property(self, web_fetch_tool):
        schema = web_fetch_tool.input_schema
        assert "url" in schema["properties"]

    def test_schema_url_is_required(self, web_fetch_tool):
        schema = web_fetch_tool.input_schema
        assert "url" in schema["required"]

    def test_schema_has_max_length_property(self, web_fetch_tool):
        schema = web_fetch_tool.input_schema
        assert "max_length" in schema["properties"]

    def test_is_read_only(self, web_fetch_tool):
        assert web_fetch_tool.is_read_only() is True

    def test_is_read_only_with_input(self, web_fetch_tool):
        assert web_fetch_tool.is_read_only({"url": "https://example.com"}) is True


class TestWebFetchToolValidation:
    """Tests for URL validation in WebFetchTool."""

    @pytest.mark.asyncio
    async def test_empty_url_returns_error(self, web_fetch_tool, context):
        result = await web_fetch_tool.execute(
            tool_input={"url": ""},
            context=context,
        )
        assert result.is_error is True
        assert "url" in result.content.lower() or "empty" in result.content.lower()

    @pytest.mark.asyncio
    async def test_invalid_url_no_scheme_returns_error(self, web_fetch_tool, context):
        result = await web_fetch_tool.execute(
            tool_input={"url": "example.com/no-scheme"},
            context=context,
        )
        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_invalid_url_no_netloc_returns_error(self, web_fetch_tool, context):
        result = await web_fetch_tool.execute(
            tool_input={"url": "http://"},
            context=context,
        )
        assert result.is_error is True


class TestWebFetchToolExecution:
    """Tests for WebFetchTool HTTP execution."""

    @pytest.mark.asyncio
    async def test_html_conversion_leaves_the_event_loop_available(self, web_fetch_tool, context, monkeypatch):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        converter_threads = []

        def convert(html, url):
            converter_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "event loop could not release the converter"
            return "# Converted\nBody"

        stream = AsyncByteStream([b"<h1>Title</h1>"], headers={"content-type": "text/html"})
        monkeypatch.setattr(web_fetch_module.httpx, "AsyncClient", lambda **kwargs: AsyncClientStreamOnly(stream))
        monkeypatch.setattr(web_fetch_module, "_extract_markdown_from_html", convert)
        task = asyncio.create_task(
            web_fetch_tool.execute(
                tool_input={"url": "https://example.com", "max_length": 5},
                context=context,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not task.done()
            assert len(converter_threads) == 1
            assert converter_threads[0] != threading.get_ident()
        finally:
            release.set()
            result = await task
        assert not result.is_error and result.content == "# Con"

    @pytest.mark.asyncio
    async def test_fetches_html_content_and_strips_tags(self, web_fetch_tool, context):
        html_response = "<html><body><h1>Hello</h1><p>World content</p></body></html>"
        stream = AsyncByteStream(
            [html_response.encode()],
            headers={"content-type": "text/html; charset=utf-8"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert "<html>" not in result.content
        assert "Hello" in result.content
        assert "World content" in result.content

    @pytest.mark.asyncio
    async def test_fetches_plain_text_content(self, web_fetch_tool, context):
        text_response = "This is plain text content."
        stream = AsyncByteStream(
            [text_response.encode()],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com/text"},
                context=context,
            )

        assert result.is_error is False
        assert "This is plain text content." in result.content

    @pytest.mark.asyncio
    async def test_truncates_to_max_length(self, web_fetch_tool, context):
        long_content = "A" * 100000
        stream = AsyncByteStream(
            [long_content.encode()],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        max_length = 1000
        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com", "max_length": max_length},
                context=context,
            )

        assert result.is_error is False
        assert len(result.content) <= max_length
        assert result.content.endswith("[truncated]")

    @pytest.mark.asyncio
    async def test_aliyun_article_is_converted_through_real_tool_execution(self, web_fetch_tool, context):
        html = """<nav>Unrelated navigation</nav><h1>Cloud guide</h1>
        <div class="markdown-body"><h2>Regions</h2><table><tr><th>Zone</th><th>Type</th></tr>
        <tr><td>J</td><td>r9i</td></tr></table><a href="/zh/ecs/api">API</a></div>"""
        stream = AsyncByteStream([html.encode()], headers={"content-type": "text/html; charset=utf-8"})
        stream.url = "https://help.aliyun.com/zh/ecs/guide"
        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=AsyncClientStreamOnly(stream)):
            result = await web_fetch_tool.execute(tool_input={"url": "https://example.com/redirect"}, context=context)

        assert result.is_error is False
        assert "# Cloud guide" in result.content
        assert "| J | r9i |" in result.content
        assert "Unrelated navigation" not in result.content
        assert "[API](https://help.aliyun.com/zh/ecs/api)" in result.content

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self, web_fetch_tool, context):
        mock_client = AsyncClientStreamOnly(stream_error=httpx.HTTPError("Connection failed"))

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_default_max_length_is_100000(self, web_fetch_tool, context):
        long_content = "B" * 110000
        stream = AsyncByteStream(
            [long_content.encode()],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert len(result.content) == 100000
        assert result.metadata["web_fetch"]["source_truncated"]

    @pytest.mark.asyncio
    async def test_streaming_stops_at_download_byte_cap(self, web_fetch_tool, context, monkeypatch):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abc", b"def", b"ghi"],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert result.content == "abcde\n\n[truncated]"
        assert stream.consumed == 2

    @pytest.mark.asyncio
    async def test_streaming_exact_download_byte_cap_is_not_truncated(self, web_fetch_tool, context, monkeypatch):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abcde"],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert result.content == "abcde"

    @pytest.mark.asyncio
    async def test_streaming_exact_cap_then_more_without_content_length_is_truncated(
        self, web_fetch_tool, context, monkeypatch
    ):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abcde", b"f"],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert result.content == "abcde\n\n[truncated]"
        assert stream.consumed == 2

    @pytest.mark.asyncio
    async def test_streaming_exact_cap_with_larger_content_length_is_truncated(
        self, web_fetch_tool, context, monkeypatch
    ):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abcde"],
            headers={"content-length": "6", "content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert result.content == "abcde\n\n[truncated]"

    @pytest.mark.asyncio
    async def test_download_cap_marker_respects_max_length(self, web_fetch_tool, context, monkeypatch):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abc", b"def"],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com", "max_length": 5},
                context=context,
            )

        assert result.is_error is False
        assert len(result.content) <= 5

    @pytest.mark.asyncio
    async def test_max_length_zero_returns_empty_content(self, web_fetch_tool, context, monkeypatch):
        monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 5)
        stream = AsyncByteStream(
            [b"abc", b"def"],
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com", "max_length": 0},
                context=context,
            )

        assert result.is_error is False
        assert len(result.content) == 0

    @pytest.mark.asyncio
    async def test_streaming_html_still_strips_tags(self, web_fetch_tool, context):
        stream = AsyncByteStream(
            [b"<html><body><p>Hello</p><script>bad()</script></body></html>"],
            headers={"content-type": "text/html; charset=utf-8"},
        )

        mock_client = AsyncClientStreamOnly(stream)

        with patch("iac_code.tools.web_fetch.httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch_tool.execute(
                tool_input={"url": "https://example.com"},
                context=context,
            )

        assert result.is_error is False
        assert "Hello" in result.content
        assert "bad" not in result.content


class TestWebFetchToolUI:
    """Tests for WebFetchTool UI rendering methods."""

    def test_render_tool_use_message(self, web_fetch_tool):
        result = web_fetch_tool.render_tool_use_message({"url": "https://example.com"})
        assert result is not None

    def test_render_tool_result_message_success(self, web_fetch_tool):
        result = web_fetch_tool.render_tool_result_message("Page content here")
        assert result is not None

    def test_render_tool_result_message_translates_truncation_summary(self, web_fetch_tool, monkeypatch):
        monkeypatch.setattr("iac_code.tools.web_fetch._", lambda message: "i18n:" + message)
        output = "\n".join("line{}".format(index) for index in range(55))

        result = web_fetch_tool.render_tool_result_message(output, verbose=True)

        assert result is not None
        assert "i18n:... (5 more lines)" in result

    def test_render_tool_result_message_error(self, web_fetch_tool):
        result = web_fetch_tool.render_tool_result_message("Error occurred", is_error=True)
        assert result is not None

    def test_render_tool_use_error_message(self, web_fetch_tool):
        result = web_fetch_tool.render_tool_use_error_message("Something went wrong")
        assert result is not None

    def test_user_facing_name(self, web_fetch_tool):
        name = web_fetch_tool.user_facing_name()
        assert isinstance(name, str)
        assert len(name) > 0

    def test_get_activity_description_with_input(self, web_fetch_tool):
        desc = web_fetch_tool.get_activity_description({"url": "https://example.com"})
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_get_activity_description_without_input(self, web_fetch_tool):
        desc = web_fetch_tool.get_activity_description()
        assert isinstance(desc, str)
        assert len(desc) > 0

    def test_get_tool_use_summary_with_input(self, web_fetch_tool):
        summary = web_fetch_tool.get_tool_use_summary({"url": "https://example.com"})
        assert summary is not None
        assert "example.com" in summary

    def test_get_tool_use_summary_without_input(self, web_fetch_tool):
        summary = web_fetch_tool.get_tool_use_summary()
        # May return None when no input
        assert summary is None or isinstance(summary, str)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_url", "official"),
    [
        ("https://help.aliyun.com/zh/ecs/user-guide/example", True),
        ("https://help.aliyun.com/en/ecs/user-guide/example", True),
        ("https://help.aliyun.com/document_detail/123.html", True),
        ("https://help.aliyun.com.evil.example/zh/ecs/example", False),
        ("https://help.aliyun.com@evil.example/zh/ecs/example", False),
        ("https://help.aliyun.com/search", False),
        ("http://help.aliyun.com/zh/ecs/example", False),
        ("https://example.com/doc", False),
    ],
)
async def test_official_classification_uses_final_url(monkeypatch, final_url, official):
    original = "article body\n" * 5000
    response = AsyncByteStream([original.encode()], headers={"content-type": "text/plain"})
    response.url = httpx.URL(final_url)
    monkeypatch.setattr(web_fetch_module.httpx, "AsyncClient", lambda **kwargs: AsyncClientStreamOnly(response))
    result = await WebFetchTool().execute(
        tool_input={"url": "https://help.aliyun.com/zh/ecs/original", "max_length": 500},
        context=ToolContext(),
    )
    assert not result.is_error
    assert result.metadata["web_fetch"]["official_document"] == official
    assert result.metadata["web_fetch"]["source_truncated"] == (not official)
    if official:
        assert result.content == original
    else:
        assert len(result.content) <= 500
        assert "[truncated]" in result.content


@pytest.mark.asyncio
async def test_official_document_still_obeys_download_cap(monkeypatch):
    monkeypatch.setattr(web_fetch_module, "MAX_DOWNLOAD_BYTES", 10)
    response = AsyncByteStream([b"x" * 100], headers={"content-type": "text/plain"})
    response.url = httpx.URL("https://help.aliyun.com/zh/ecs/example")
    monkeypatch.setattr(web_fetch_module.httpx, "AsyncClient", lambda **kwargs: AsyncClientStreamOnly(response))
    result = await WebFetchTool().execute(
        tool_input={"url": str(response.url)},
        context=ToolContext(),
    )
    assert result.metadata["web_fetch"]["source_truncated"]
    assert result.content == "x" * 10 + web_fetch_module.TRUNCATION_MARKER
