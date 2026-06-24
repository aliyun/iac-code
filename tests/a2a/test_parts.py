from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from a2a.types import Part
from google.protobuf.struct_pb2 import Value
from PIL import Image

from iac_code.a2a import parts
from iac_code.a2a.parts import parts_to_pipeline_input
from iac_code.agent.message import ImageBlock, TextBlock


def _data_part(value: dict[str, object]) -> Part:
    data = Value()
    data.struct_value.update(value)
    return Part(data=data, media_type="application/json")


def _binary_data_part(value: dict[str, object], *, media_type: str) -> Part:
    data = Value()
    data.struct_value.update(value)
    return Part(data=data, media_type=media_type)


def test_text_part_defaults_to_plain_text(tmp_path) -> None:
    assert parts.part_to_prompt(Part(text="create a vpc"), cwd=tmp_path) == "create a vpc"


def test_text_part_accepts_advertised_text_like_media_type(tmp_path) -> None:
    part = Part(text="# Review this template", media_type="text/markdown")

    assert parts.part_to_prompt(part, cwd=tmp_path) == "# Review this template"


def test_text_part_accepts_extra_text_mime_type_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IACCODE_A2A_TEXT_MIME_TYPES", "application/vnd.iac+yaml")
    part = Part(text="Resources: {}", media_type="application/vnd.iac+yaml")

    assert "application/vnd.iac+yaml" in parts.supported_input_mime_types()
    assert parts.part_to_prompt(part, cwd=tmp_path) == "Resources: {}"


def test_data_part_serializes_compact_json(tmp_path) -> None:
    assert parts.part_to_prompt(_data_part({"template": "value", "count": 2}), cwd=tmp_path) == (
        '{"count":2.0,"template":"value"}'
    )


def test_raw_part_accepts_utf8_text_like_media_type(tmp_path) -> None:
    part = Part(raw="name: vpc\n".encode(), media_type="text/yaml")

    assert parts.part_to_prompt(part, cwd=tmp_path) == "name: vpc\n"


def test_file_url_part_reads_text_file_inside_workspace(tmp_path) -> None:
    source = tmp_path / "template.yaml"
    source.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")

    assert parts.part_to_prompt(Part(url=source.as_uri(), media_type="text/plain"), cwd=tmp_path) == (
        "ROSTemplateFormatVersion: '2015-09-01'\n"
    )


def test_raw_image_part_adds_multimodal_manifest(tmp_path) -> None:
    raw_png = b"\x89PNG\r\n\x1a\nimage-bytes"

    prompt = parts.part_to_prompt(Part(raw=raw_png, media_type="image/png", filename="diagram.png"), cwd=tmp_path)

    assert "A2A multimodal attachment:" in prompt
    assert "filename=diagram.png" in prompt
    assert "mediaType=image/png" in prompt
    assert "byteSize=19" in prompt
    assert "sha256=" in prompt
    assert "image-bytes" not in prompt


def test_file_url_audio_part_adds_multimodal_manifest(tmp_path) -> None:
    source = tmp_path / "voice.wav"
    source.write_bytes(b"RIFFaudio")

    prompt = parts.part_to_prompt(Part(url=source.as_uri(), media_type="audio/wav"), cwd=tmp_path)

    assert "A2A multimodal attachment:" in prompt
    assert "filename=voice.wav" in prompt
    assert "mediaType=audio/wav" in prompt
    assert "byteSize=9" in prompt
    assert f"source={source.as_uri()}" in prompt


def test_resolve_workspace_path_falls_back_for_absolute_path_when_process_cwd_is_deleted(monkeypatch, tmp_path) -> None:
    def deleted_process_cwd_failure(self):
        raise FileNotFoundError("[Errno 2] No such file or directory")

    monkeypatch.setattr(parts.Path, "resolve", deleted_process_cwd_failure)

    assert parts.resolve_workspace_path(tmp_path) == tmp_path.absolute()


def test_resolve_workspace_path_does_not_fallback_through_symlink_when_process_cwd_is_deleted(
    monkeypatch, tmp_path
) -> None:
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)

    def deleted_process_cwd_failure(self):
        raise FileNotFoundError("[Errno 2] No such file or directory")

    monkeypatch.setattr(parts.Path, "resolve", deleted_process_cwd_failure)

    with pytest.raises(FileNotFoundError):
        parts.resolve_workspace_path(link)


def test_binary_data_part_decodes_base64_manifest(tmp_path) -> None:
    encoded = base64.b64encode(b"\x00\x01binary").decode("ascii")

    prompt = parts.part_to_prompt(
        _binary_data_part({"filename": "sample.bin", "bytes": encoded}, media_type="application/octet-stream"),
        cwd=tmp_path,
    )

    assert "filename=sample.bin" in prompt
    assert "mediaType=application/octet-stream" in prompt
    assert "byteSize=8" in prompt


@pytest.mark.parametrize(
    ("part", "message"),
    [
        (Part(text="bad", media_type="application/octet-stream"), "unsupported media type"),
        (Part(raw=b"\xff", media_type="text/plain"), "UTF-8"),
        (Part(url="https://example.com/template.yaml", media_type="text/plain"), "local file://"),
        (Part(url="http://127.0.0.1/template.yaml", media_type="text/plain"), "local file://"),
    ],
)
def test_part_rejects_unsupported_or_unsafe_inputs(part: Part, message: str, tmp_path) -> None:
    with pytest.raises(ValueError, match=message):
        parts.part_to_prompt(part, cwd=tmp_path)


def test_file_url_rejects_path_traversal_outside_workspace(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the allowed workspace") as exc_info:
        parts.part_to_prompt(Part(url=outside.as_uri(), media_type="text/plain"), cwd=tmp_path)

    assert str(outside) not in str(exc_info.value)


def test_file_url_rejects_symlink_escape_without_leaking_path(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="outside the allowed workspace") as exc_info:
        parts.part_to_prompt(Part(url=link.as_uri(), media_type="text/plain"), cwd=tmp_path)

    assert str(outside) not in str(exc_info.value)
    assert str(link) not in str(exc_info.value)


@pytest.mark.parametrize("name", ["missing.txt", "directory"])
def test_file_url_rejects_missing_files_and_directories(name: str, tmp_path) -> None:
    path = tmp_path / name
    if name == "directory":
        path.mkdir()

    with pytest.raises(ValueError, match="existing file"):
        parts.part_to_prompt(Part(url=path.as_uri(), media_type="text/plain"), cwd=tmp_path)


def test_inline_raw_data_and_file_content_size_limits(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(parts, "MAX_INLINE_BYTES", 3)
    monkeypatch.setattr(parts, "MAX_FILE_BYTES", 3)
    source = tmp_path / "large.txt"
    source.write_text("abcd", encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        parts.part_to_prompt(Part(raw=b"abcd", media_type="text/plain"), cwd=tmp_path)

    with pytest.raises(ValueError, match="too large"):
        parts.part_to_prompt(_data_part({"abcd": "efgh"}), cwd=tmp_path)

    with pytest.raises(ValueError, match="too large"):
        parts.part_to_prompt(Part(url=source.as_uri(), media_type="text/plain"), cwd=tmp_path)


def test_message_parts_join_non_empty_values(tmp_path) -> None:
    assert parts.parts_to_prompt([Part(text="first"), Part(text=""), Part(text="second")], cwd=tmp_path) == (
        "first\nsecond"
    )


def _resize_spy(monkeypatch, *, output: bytes, media_type: str = "image/webp") -> list[bytes]:
    calls: list[bytes] = []

    def fake_resize(content: bytes):
        calls.append(content)
        return SimpleNamespace(data=output, media_type=media_type)

    monkeypatch.setattr("iac_code.a2a.parts.maybe_resize_and_downsample", fake_resize)
    return calls


def _tiny_bmp_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(buf, format="BMP")
    return buf.getvalue()


def _tiny_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(0, 255, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_parts_to_pipeline_input_converts_raw_image(monkeypatch, tmp_path) -> None:
    raw = b"fake png bytes"
    resized = b"resized raw image"
    calls = _resize_spy(monkeypatch, output=resized, media_type="image/webp")

    value = parts_to_pipeline_input([Part(raw=raw, media_type="image/png")], cwd=tmp_path)

    assert calls == [raw]
    assert value.has_images is True
    assert value.display_text == "[Image input]"
    assert value.content == [ImageBlock(media_type="image/webp", data=base64.b64encode(resized).decode("ascii"))]


def test_parts_to_pipeline_input_preserves_text_plus_image_order(monkeypatch, tmp_path) -> None:
    raw = b"fake jpeg bytes"
    resized = b"resized jpeg bytes"
    calls = _resize_spy(monkeypatch, output=resized, media_type="image/jpeg")

    value = parts_to_pipeline_input(
        [
            Part(text="inspect this", media_type="text/plain"),
            Part(raw=raw, media_type="image/jpeg"),
        ],
        cwd=tmp_path,
    )

    assert calls == [raw]
    assert value.display_text == "inspect this"
    assert value.content == [
        TextBlock(text="inspect this"),
        ImageBlock(media_type="image/jpeg", data=base64.b64encode(resized).decode("ascii")),
    ]


def test_parts_to_pipeline_input_converts_base64_data_image(monkeypatch, tmp_path) -> None:
    raw = b"fake data image"
    resized = b"resized data image"
    encoded = base64.b64encode(raw).decode("ascii")
    calls = _resize_spy(monkeypatch, output=resized, media_type="image/png")

    value = parts_to_pipeline_input([_binary_data_part({"bytes": encoded}, media_type="image/png")], cwd=tmp_path)

    assert calls == [raw]
    assert value.content == [ImageBlock(media_type="image/png", data=base64.b64encode(resized).decode("ascii"))]


def test_parts_to_pipeline_input_converts_safe_file_url_image(monkeypatch, tmp_path) -> None:
    raw = b"file image bytes"
    resized = b"resized file image"
    source = tmp_path / "diagram.png"
    source.write_bytes(raw)
    calls = _resize_spy(monkeypatch, output=resized, media_type="image/png")

    value = parts_to_pipeline_input([Part(url=source.as_uri(), media_type="image/png")], cwd=tmp_path)

    assert calls == [raw]
    assert value.content == [ImageBlock(media_type="image/png", data=base64.b64encode(resized).decode("ascii"))]


def test_parts_to_pipeline_input_uses_real_resizer_for_valid_image_bytes(tmp_path) -> None:
    raw = _tiny_bmp_bytes()

    value = parts_to_pipeline_input([Part(raw=raw, media_type="image/png")], cwd=tmp_path)

    assert isinstance(value.content, list)
    block = value.content[0]
    assert isinstance(block, ImageBlock)
    assert block.media_type == "image/png"
    assert base64.b64decode(block.data).startswith(b"\x89PNG\r\n\x1a\n")


def test_parts_to_pipeline_input_accepts_tiny_png_without_monkeypatch(tmp_path) -> None:
    raw = _tiny_png_bytes()

    value = parts_to_pipeline_input([Part(raw=raw, media_type="image/png")], cwd=tmp_path)

    assert isinstance(value.content, list)
    block = value.content[0]
    assert isinstance(block, ImageBlock)
    assert block.media_type == "image/png"
    assert base64.b64decode(block.data).startswith(b"\x89PNG\r\n\x1a\n")


def test_parts_to_pipeline_input_rejects_unsafe_file_url_image(tmp_path) -> None:
    outside = tmp_path.parent / "outside-diagram.png"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="outside the allowed workspace"):
        parts_to_pipeline_input([Part(url=outside.as_uri(), media_type="image/png")], cwd=tmp_path)


def test_parts_to_pipeline_input_rejects_invalid_base64_data_image(tmp_path) -> None:
    with pytest.raises(ValueError, match="valid base64"):
        parts_to_pipeline_input([_binary_data_part({"bytes": "not-base64!"}, media_type="image/png")], cwd=tmp_path)


def test_parts_to_pipeline_input_rejects_oversized_raw_image(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("iac_code.a2a.parts.MAX_BINARY_INLINE_BYTES", 3)

    with pytest.raises(ValueError, match="too large"):
        parts_to_pipeline_input([Part(raw=b"abcd", media_type="image/png")], cwd=tmp_path)


def test_parts_to_pipeline_input_rejects_audio_as_true_image(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported image media type"):
        parts_to_pipeline_input([Part(raw=b"audio", media_type="audio/wav")], cwd=tmp_path)
