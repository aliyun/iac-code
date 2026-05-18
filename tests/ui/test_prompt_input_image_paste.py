from unittest.mock import MagicMock

from iac_code.ui.core.key_event import KeyEvent
from iac_code.ui.core.prompt_input import PromptInput, PromptInputResult
from iac_code.utils.image.pasted_content import PastedContent


def test_attach_image_inserts_placeholder_at_cursor():
    pi = PromptInput(keybinding_manager=MagicMock())
    pi._set_text("hello  world")
    pi._cursor = 6  # 两个空格之间
    pi.attach_image(PastedContent(id=1, type="image", content="aGVsbG8=", media_type="image/png"))
    assert pi._get_text() == "hello [Image #1] world"
    assert pi._cursor == len("hello [Image #1]")
    assert pi._pasted_contents[1].id == 1


def test_submit_returns_text_and_pasted_contents():
    pi = PromptInput(keybinding_manager=MagicMock())
    pi._set_text("see [Image #1]")
    pi._pasted_contents = {1: PastedContent(id=1, type="image", content="x", media_type="image/png")}
    result = pi.make_result()
    assert isinstance(result, PromptInputResult)
    assert result.text == "see [Image #1]"
    assert 1 in result.pasted_contents


def test_next_paste_id_monotonic():
    pi = PromptInput(keybinding_manager=MagicMock())
    assert pi.next_paste_id() == 1
    assert pi.next_paste_id() == 2


def test_attach_image_does_not_duplicate_existing_id():
    pi = PromptInput(keybinding_manager=MagicMock())
    pc1 = PastedContent(id=1, type="image", content="a", media_type="image/png")
    pi.attach_image(pc1)
    # Second attach with same id should be a no-op (idempotent)
    pi.attach_image(pc1)
    text = pi._get_text()
    assert text.count("[Image #1]") == 1


def test_highlight_image_refs_wraps_known_ids():
    pi = PromptInput(keybinding_manager=MagicMock())
    pi._pasted_contents = {1: PastedContent(id=1, type="image", content="x", media_type="image/png")}
    out = pi._highlight_image_refs("see [Image #1] and [Image #99]")
    # Known id wrapped, unknown id untouched
    assert "\033[36m[Image #1]\033[0m" in out
    assert "[Image #99]" in out
    # Unknown should not be wrapped
    assert "\033[36m[Image #99]" not in out


def test_highlight_image_refs_returns_unchanged_when_empty():
    pi = PromptInput(keybinding_manager=MagicMock())
    assert pi._highlight_image_refs("plain text") == "plain text"


def test_bracketed_paste_invokes_paste_handler():
    """When a paste_handler is supplied, bracketed-paste events route through
    it before any text insertion happens."""
    seen: list[str] = []

    def handler(text: str) -> bool:
        seen.append(text)
        return False

    pi = PromptInput(keybinding_manager=MagicMock(), paste_handler=handler)
    pi._handle_key(KeyEvent(key="paste", char="hello world"))
    assert seen == ["hello world"]
    # Handler returned False → text inserted normally
    assert pi._get_text() == "hello world"


def test_bracketed_paste_handler_consumed_suppresses_text_insert():
    """When paste_handler returns True, the text MUST NOT be inserted into
    the buffer (the handler attached the content out-of-band, e.g. as an
    image placeholder)."""
    pi = PromptInput(
        keybinding_manager=MagicMock(),
        paste_handler=lambda _text: True,
    )
    pi._handle_key(KeyEvent(key="paste", char="ignored content"))
    assert pi._get_text() == ""


def test_bracketed_paste_without_handler_inserts_text():
    """Existing behavior preserved when paste_handler is not supplied."""
    pi = PromptInput(keybinding_manager=MagicMock())
    pi._handle_key(KeyEvent(key="paste", char="some text"))
    assert pi._get_text() == "some text"
