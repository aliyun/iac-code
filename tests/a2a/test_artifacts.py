import pytest

from iac_code.a2a.artifacts import A2AArtifactStore, UnsafeArtifactNameError


def test_artifact_store_writes_text_and_metadata(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_text(
        filename="template.yaml",
        content="ROSTemplateFormatVersion: '2015-09-01'",
        media_type="text/yaml",
    )

    assert metadata.filename == "template.yaml"
    assert metadata.byte_size > 0
    assert metadata.sha256
    assert metadata.uri.startswith("file://")
    assert store.path_for(metadata.artifact_id).read_text(encoding="utf-8").startswith("ROSTemplate")


def test_artifact_store_writes_binary_and_metadata(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_bytes(filename="diagram.png", content=b"\x89PNG\r\n\x1a\nimage", media_type="image/png")

    assert metadata.filename == "diagram.png"
    assert metadata.media_type == "image/png"
    assert metadata.byte_size == 13
    assert metadata.sha256
    assert metadata.uri.startswith("file://")
    assert store.path_for(metadata.artifact_id).read_bytes() == b"\x89PNG\r\n\x1a\nimage"


def test_artifact_store_decodes_base64_content(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_base64(filename="sample.bin", content="AAFiYXNlNjQ=", media_type="application/octet-stream")

    assert metadata.byte_size == 8
    assert store.path_for(metadata.artifact_id).read_bytes() == b"\x00\x01base64"


def test_artifact_store_rejects_path_traversal(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    with pytest.raises(UnsafeArtifactNameError):
        store.save_text(filename="../secret.txt", content="bad", media_type="text/plain")
