from iac_code.utils.image.pasted_content import (
    PastedContent,
    format_image_ref,
    parse_image_refs,
)


def test_format_image_ref():
    assert format_image_ref(3) == "[Image #3]"


def test_parse_image_refs_at_arbitrary_positions():
    text = "look at [Image #1] and also [Image #4] and [Image #1] again"
    refs = parse_image_refs(text)
    assert [r.id for r in refs] == [1, 4, 1]
    assert refs[1].start == text.index("[Image #4]")


def test_pasted_content_image_validation():
    pc = PastedContent(id=1, type="image", content="aGVsbG8=", media_type="image/png")
    assert pc.is_valid_image() is True
    assert PastedContent(id=2, type="image", content="").is_valid_image() is False
