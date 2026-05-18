from __future__ import annotations

import base64

import pytest
from a2a.types import Part
from google.protobuf.struct_pb2 import Value

from iac_code.a2a import parts


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
