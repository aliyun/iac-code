"""Tests for the WebFetchTool."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import iac_code.tools.web_fetch as web_fetch_module
from iac_code.tools.base import ToolContext
from iac_code.tools.web_fetch import WebFetchTool, _extract_text_from_html


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
    async def test_default_max_length_is_50000(self, web_fetch_tool, context):
        # Content longer than default 50000
        long_content = "B" * 60000
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
        assert len(result.content) <= 50000

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
