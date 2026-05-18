import base64

from iac_code.agent.message import ImageBlock, TextBlock
from iac_code.utils.image.pasted_content import PastedContent
from iac_code.utils.image.processor import process_user_input


def _b64_png(w=10, h=10):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=(0, 0, 0)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_text_only_returns_single_text_block():
    blocks = process_user_input("hello world", pasted_contents={})
    assert blocks == [TextBlock(text="hello world")]


def test_image_at_arbitrary_position_produces_interleaved_blocks():
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    blocks = process_user_input("look at [Image #1] please", pasted_contents=pc)
    assert len(blocks) == 3
    assert isinstance(blocks[0], TextBlock) and blocks[0].text == "look at "
    assert isinstance(blocks[1], ImageBlock)
    assert isinstance(blocks[2], TextBlock) and blocks[2].text == " please"


def test_multiple_images_preserve_order():
    pc = {
        1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png"),
        2: PastedContent(id=2, type="image", content=_b64_png(), media_type="image/png"),
    }
    blocks = process_user_input("[Image #2][Image #1]", pasted_contents=pc)
    image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    assert image_blocks == [blocks[0], blocks[1]]


def test_unknown_image_ref_kept_as_text():
    blocks = process_user_input("see [Image #99]", pasted_contents={})
    assert blocks == [TextBlock(text="see [Image #99]")]


def test_image_at_position_zero():
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    blocks = process_user_input("[Image #1] caption", pasted_contents=pc)
    # No leading empty text block
    assert isinstance(blocks[0], ImageBlock)
    assert isinstance(blocks[1], TextBlock) and blocks[1].text == " caption"
    assert all(not (isinstance(b, TextBlock) and b.text == "") for b in blocks)


def test_image_at_end_of_string():
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    blocks = process_user_input("look at [Image #1]", pasted_contents=pc)
    assert isinstance(blocks[0], TextBlock) and blocks[0].text == "look at "
    assert isinstance(blocks[1], ImageBlock)
    assert len(blocks) == 2  # no trailing empty text


def test_empty_text_returns_empty_list():
    blocks = process_user_input("", pasted_contents={})
    assert blocks == []


def test_invalid_paste_entry_treated_as_text():
    # PastedContent exists but is text/empty — should be treated like an unknown ref.
    pc = {
        1: PastedContent(id=1, type="text", content="not an image"),
        2: PastedContent(id=2, type="image", content="", media_type="image/png"),
    }
    blocks = process_user_input("a [Image #1] b [Image #2] c", pasted_contents=pc)
    # Both should be preserved as plain text — single text block carries everything
    assert blocks == [TextBlock(text="a [Image #1] b [Image #2] c")]


def test_mixed_valid_and_unknown_ids():
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type="image/png")}
    blocks = process_user_input("see [Image #1] and [Image #99]", pasted_contents=pc)
    # The valid ref becomes ImageBlock; the unknown one stays as text in the trailing TextBlock.
    assert isinstance(blocks[0], TextBlock) and blocks[0].text == "see "
    assert isinstance(blocks[1], ImageBlock)
    assert isinstance(blocks[2], TextBlock) and blocks[2].text == " and [Image #99]"


def test_processor_passes_through_data_without_recompressing(monkeypatch):
    """REPL already resized; processor must not Pillow-decode/re-encode again."""
    import iac_code.utils.image.resizer as resizer_mod

    def _boom(*_a, **_kw):
        raise AssertionError("processor should not call maybe_resize_and_downsample")

    monkeypatch.setattr(resizer_mod, "maybe_resize_and_downsample", _boom)

    payload = _b64_png()
    pc = {1: PastedContent(id=1, type="image", content=payload, media_type="image/jpeg")}
    blocks = process_user_input("[Image #1]", pasted_contents=pc)

    assert len(blocks) == 1
    assert isinstance(blocks[0], ImageBlock)
    assert blocks[0].data == payload  # passthrough, not re-encoded
    assert blocks[0].media_type == "image/jpeg"


def test_processor_defaults_media_type_when_missing():
    """If PastedContent.media_type is None, fall back to image/png."""
    pc = {1: PastedContent(id=1, type="image", content=_b64_png(), media_type=None)}
    blocks = process_user_input("[Image #1]", pasted_contents=pc)
    assert isinstance(blocks[0], ImageBlock)
    assert blocks[0].media_type == "image/png"
