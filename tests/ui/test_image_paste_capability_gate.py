import io
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from iac_code.utils.image.clipboard import ClipboardImage
from iac_code.utils.image.pasted_content import PastedContent


def _valid_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def repl():
    """Construct a minimally-stubbed REPL for unit testing handle_image_paste."""
    from iac_code.ui.repl import InlineREPL

    repl = InlineREPL.__new__(InlineREPL)
    repl._current_model = "claude-opus-4-7"
    repl._prompt_input = MagicMock()
    repl._prompt_input.next_paste_id.return_value = 7
    repl.renderer = MagicMock()
    repl.console = MagicMock()
    repl._image_store = MagicMock()
    repl._image_store.store.return_value = "/tmp/fake/7.png"
    return repl


def test_ctrl_v_no_image_is_noop(repl):
    from iac_code.ui.repl import handle_image_paste

    with patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=None):
        result = handle_image_paste(repl)
    assert result is False
    repl._prompt_input.attach_image.assert_not_called()


def test_ctrl_v_no_image_shows_system_message(repl):
    """Ctrl+V is an explicit 'paste image' intent. When the clipboard is
    empty, the user must see *something* — silent return is the original
    bug. The wrapper schedules a system message and consumes the event so
    the raw \\x16 byte never falls through to the printable check."""
    # handle_image_paste lazy-imports get_image_from_clipboard from its source
    # module, so the patch must target the source not the repl alias.
    with patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=None):
        consumed = repl._handle_ctrl_v_image()
    assert consumed is True
    repl._prompt_input.schedule_action.assert_called_once()
    # The scheduled lambda routes the message through renderer.print_system_message
    repl._prompt_input.schedule_action.call_args.args[0]()
    repl.renderer.print_system_message.assert_called_once()
    msg = repl.renderer.print_system_message.call_args.args[0]
    assert "image" in msg.lower() and "clipboard" in msg.lower()


def test_ctrl_v_attaches_image_when_supported(repl):
    img_bytes = _valid_png_bytes()
    img = ClipboardImage(data=img_bytes, media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        from iac_code.ui.repl import handle_image_paste

        result = handle_image_paste(repl)
    assert result is True
    repl._prompt_input.attach_image.assert_called_once()
    pc_arg = repl._prompt_input.attach_image.call_args.args[0]
    assert isinstance(pc_arg, PastedContent)
    assert pc_arg.id == 7
    assert pc_arg.type == "image"
    repl._image_store.store.assert_called_once()
    # Happy path: no warnings scheduled
    repl._prompt_input.schedule_action.assert_not_called()


def test_ctrl_v_warns_when_model_unsupported(repl):
    img = ClipboardImage(data=_valid_png_bytes(), media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=False),
    ):
        from iac_code.ui.repl import handle_image_paste

        result = handle_image_paste(repl)
    # Returning True means "consumed" — we did NOT fall through to text paste.
    assert result is True
    repl._prompt_input.attach_image.assert_not_called()
    # Warning routed through schedule_action so it fires outside raw mode
    repl._prompt_input.schedule_action.assert_called_once()
    # Invoking the scheduled lambda should call the renderer
    repl._prompt_input.schedule_action.call_args.args[0]()
    repl.renderer.print_system_message.assert_called_once()


def test_ctrl_v_warns_on_resize_error(repl):
    from iac_code.utils.image.resizer import ImageResizeError

    img = ClipboardImage(data=_valid_png_bytes(), media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
        patch(
            "iac_code.utils.image.resizer.maybe_resize_and_downsample",
            side_effect=ImageResizeError("boom"),
        ),
    ):
        from iac_code.ui.repl import handle_image_paste

        result = handle_image_paste(repl)
    assert result is True
    repl._prompt_input.attach_image.assert_not_called()
    # Warning routed through schedule_action so it fires outside raw mode
    repl._prompt_input.schedule_action.assert_called_once()
    # Invoking the scheduled lambda should call the renderer
    repl._prompt_input.schedule_action.call_args.args[0]()
    repl.renderer.print_system_message.assert_called_once()


def test_ctrl_v_warns_when_store_fails(repl):
    repl._image_store.store.return_value = None  # simulate disk failure
    img = ClipboardImage(data=_valid_png_bytes(), media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch("iac_code.services.capabilities.multimodal.is_model_multimodal", return_value=True),
    ):
        from iac_code.ui.repl import handle_image_paste

        result = handle_image_paste(repl)
    assert result is True
    # attach_image still happens (we have the image in memory)
    repl._prompt_input.attach_image.assert_called_once()
    # ... and a warning was scheduled
    repl._prompt_input.schedule_action.assert_called_once()


def test_ctrl_v_passes_provider_context_to_is_model_multimodal(repl):
    """When provider is openai_compatible, the call must include base_url + api_key
    so auto-detect can probe."""
    repl._current_model = "custom-vl"
    repl._current_provider_config = {
        "keyName": "openai_compatible",
        "apiBase": "https://example.com/v1",
    }
    repl._credentials = {"openai_compatible": "sk-test"}
    img = ClipboardImage(data=_valid_png_bytes(), media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch(
            "iac_code.services.capabilities.multimodal.is_model_multimodal",
            return_value=False,
        ) as mock_capability,
    ):
        from iac_code.ui.repl import handle_image_paste

        handle_image_paste(repl)
    mock_capability.assert_called_once_with(
        "custom-vl",
        provider_key="openai_compatible",
        base_url="https://example.com/v1",
        api_key="sk-test",
    )


def test_ctrl_v_handles_missing_provider_context_gracefully(repl):
    """If _current_provider_config / _credentials aren't set on the stub, capability
    check still runs with all-None kwargs (matches old behavior, no crash)."""
    # The default fixture doesn't set these, so simulate that by deleting
    if hasattr(repl, "_current_provider_config"):
        del repl._current_provider_config
    if hasattr(repl, "_credentials"):
        del repl._credentials
    img = ClipboardImage(data=_valid_png_bytes(), media_type="image/png")
    with (
        patch("iac_code.utils.image.clipboard.get_image_from_clipboard", return_value=img),
        patch(
            "iac_code.services.capabilities.multimodal.is_model_multimodal",
            return_value=True,
        ),
        patch("iac_code.utils.image.resizer.maybe_resize_and_downsample") as mock_resize,
    ):
        from types import SimpleNamespace

        mock_resize.return_value = SimpleNamespace(
            data=b"\x89PNG\r\n\x1a\n",
            media_type="image/png",
            dimensions=SimpleNamespace(),
        )
        from iac_code.ui.repl import handle_image_paste

        result = handle_image_paste(repl)
    assert result is True  # No crash on missing context
