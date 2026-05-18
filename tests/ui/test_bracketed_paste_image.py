"""Tests for InlineREPL._on_bracketed_paste — the Cmd+V image-paste path.

On macOS, Cmd+V is intercepted by the terminal and forwarded as a bracketed
paste sequence (never reaches a Ctrl+V keybinding). The hook here probes the
system clipboard on every bracketed paste; if an image is present, attach it
and decide whether the accompanying text should also be inserted.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from iac_code.utils.image.clipboard import ClipboardImage


def _valid_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def repl():
    from iac_code.ui.repl import InlineREPL

    r = InlineREPL.__new__(InlineREPL)
    r._current_model = "claude-opus-4-7"
    r._prompt_input = MagicMock()
    r._prompt_input.next_paste_id.return_value = 1
    r.renderer = MagicMock()
    r.console = MagicMock()
    r._image_store = MagicMock()
    r._image_store.store.return_value = "/tmp/fake/1.png"
    return r


def _png_clipboard_image() -> ClipboardImage:
    return ClipboardImage(data=_valid_png_bytes(), media_type="image/png")


def test_bracketed_paste_attaches_image_and_suppresses_when_text_empty(repl):
    """Cmd+V on a screenshot: clipboard has image, bracketed-paste text is
    empty (nothing to insert). We attach the image and report consumed=True."""
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch(
            "iac_code.ui.repl.get_image_from_clipboard",
            return_value=_png_clipboard_image(),
        ),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        consumed = repl._on_bracketed_paste("")
    assert consumed is True
    repl._prompt_input.attach_image.assert_called_once()


def test_bracketed_paste_keeps_caption_alongside_image(repl):
    """Image attached + non-trivial text → we attach AND leave the caller to
    insert the text so the user keeps their accompanying caption."""
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch(
            "iac_code.ui.repl.get_image_from_clipboard",
            return_value=_png_clipboard_image(),
        ),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        consumed = repl._on_bracketed_paste("what's wrong with this screenshot?")
    assert consumed is False
    repl._prompt_input.attach_image.assert_called_once()


def test_bracketed_paste_suppresses_image_path_text(repl):
    """User pastes a Finder-copied image file: bracketed paste carries the
    quoted POSIX path, clipboard also exposes the image bytes (or
    try_read_image_from_path resolves the path). Path text would just be
    redundant noise → suppress."""
    img = ClipboardImage(
        data=_valid_png_bytes(),
        media_type="image/png",
        source_path="/Users/x/foo.png",
    )
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        consumed = repl._on_bracketed_paste("'/Users/x/foo.png'")
    assert consumed is True
    repl._prompt_input.attach_image.assert_called_once()


def test_bracketed_paste_with_no_image_returns_false(repl):
    """Plain-text paste: no image anywhere → we never attach, never suppress."""
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch("iac_code.ui.repl.get_image_from_clipboard", return_value=None),
    ):
        consumed = repl._on_bracketed_paste("just plain text")
    assert consumed is False
    repl._prompt_input.attach_image.assert_not_called()


def test_bracketed_paste_file_url_text_suppresses(repl):
    """file:// URL pasted alongside image bytes → suppress URL noise."""
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch(
            "iac_code.ui.repl.get_image_from_clipboard",
            return_value=_png_clipboard_image(),
        ),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        consumed = repl._on_bracketed_paste("file:///Users/x/foo.png")
    assert consumed is True


def test_bracketed_paste_strips_orphan_focus_events(repl):
    """When the terminal interleaves focus events around the paste boundary
    (Cmd+V triggers app focus → terminal sends \\x1b[I before/after the paste
    markers), our raw-input may capture the focus byte inside the paste
    content. After stripping, the content is effectively empty → probe
    clipboard and attach as if it were the empty-Cmd+V case."""
    img = _png_clipboard_image()
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch("iac_code.ui.repl.get_image_from_clipboard", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        # Focus-in only
        consumed = repl._on_bracketed_paste("\x1b[I")
    assert consumed is True
    repl._prompt_input.attach_image.assert_called_once()


def test_bracketed_paste_empty_after_focus_strip_does_not_insert_garbage(repl):
    """Specifically: even when no image is in the clipboard, paste content
    that's only focus events must NOT be inserted as text into the buffer
    (would surface as `\\x1b[I` garbage chars)."""
    with (
        patch("iac_code.ui.repl.try_read_image_from_path", return_value=None),
        patch("iac_code.ui.repl.get_image_from_clipboard", return_value=None),
    ):
        consumed = repl._on_bracketed_paste("\x1b[I\x1b[O")
    # Consumed=True even without image — text was pure noise; suppress insert.
    assert consumed is True
