import pytest

from iac_code.a2a.artifacts import (
    A2AArtifactStore,
    UnsafeArtifactNameError,
    sanitize_public_artifact_data,
    sanitize_public_artifact_text,
    sanitize_public_tool_output_data,
)


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
    assert metadata.uri.startswith(f"iac-code-artifact://{metadata.artifact_id}/")
    assert str(tmp_path) not in metadata.uri
    assert store.path_for(metadata.artifact_id).read_text(encoding="utf-8").startswith("ROSTemplate")


def test_artifact_store_writes_binary_and_metadata(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_bytes(filename="diagram.png", content=b"\x89PNG\r\n\x1a\nimage", media_type="image/png")

    assert metadata.filename == "diagram.png"
    assert metadata.media_type == "image/png"
    assert metadata.byte_size == 13
    assert metadata.sha256
    assert metadata.uri.startswith(f"iac-code-artifact://{metadata.artifact_id}/")
    assert str(tmp_path) not in metadata.uri
    assert store.path_for(metadata.artifact_id).read_bytes() == b"\x89PNG\r\n\x1a\nimage"


def test_artifact_store_decodes_base64_content(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_base64(filename="sample.bin", content="AAFiYXNlNjQ=", media_type="application/octet-stream")

    assert metadata.byte_size == 8
    assert store.path_for(metadata.artifact_id).read_bytes() == b"\x00\x01base64"


def test_artifact_store_normalizes_windows_path_filename(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_text(
        filename=r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
        content="ROSTemplate",
        media_type="text/yaml",
    )

    assert metadata.filename == "template.yaml"
    assert "%5CUsers" not in metadata.uri
    assert ".iac-code" not in metadata.uri
    assert store.path_for(metadata.artifact_id).read_text(encoding="utf-8") == "ROSTemplate"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("bad:name.yaml", "bad_name.yaml"),
        ("CON", "_CON"),
        ("NUL.txt", "_NUL.txt"),
        ("COM\N{SUPERSCRIPT ONE}.txt", "_COM\N{SUPERSCRIPT ONE}.txt"),
        ("LPT\N{SUPERSCRIPT THREE}.log", "_LPT\N{SUPERSCRIPT THREE}.log"),
        ("file.", "file"),
        ("a<b>.txt", "a_b_.txt"),
        ("foo|bar.yaml", "foo_bar.yaml"),
        ("template?.yaml", "template_.yaml"),
    ],
)
def test_artifact_store_normalizes_windows_reserved_or_invalid_filename(tmp_path, filename, expected) -> None:
    store = A2AArtifactStore(tmp_path)

    metadata = store.save_text(filename=filename, content="artifact", media_type="text/plain")

    assert metadata.filename == expected
    assert store.path_for(metadata.artifact_id).read_text(encoding="utf-8") == "artifact"


def test_artifact_store_rejects_path_traversal(tmp_path) -> None:
    store = A2AArtifactStore(tmp_path)

    with pytest.raises(UnsafeArtifactNameError):
        store.save_text(filename="../secret.txt", content="bad", media_type="text/plain")


def test_sanitize_public_artifact_scalar_preserves_valid_opaque_uri() -> None:
    uri = "iac-code-artifact://artifact-1/template.yaml"

    assert sanitize_public_artifact_data(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        r"iac-code-artifact://artifact-1/C:\Users\alice\.iac-code\projects\demo\template.yaml",
        "iac-code-artifact://C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo/template.yaml",
        "iac-code-artifact%3A%2F%2Fartifact-1%2FC%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo%5Ctemplate.yaml",
        "iac-code-artifact://../template.yaml",
        "iac-code-artifact://./template.yaml",
        "iac-code-artifact://artifact-1/CON.txt",
        "iac-code-artifact://artifact-1/bad%3Aname.yaml",
        "iac-code-artifact://artifact-1/name%20.",
    ],
)
def test_sanitize_public_artifact_rejects_malformed_opaque_uri(uri) -> None:
    assert sanitize_public_artifact_data(uri) == "[PATH]"
    assert sanitize_public_artifact_data({"uri": uri}) == {}


def test_sanitize_public_artifact_decodes_percent_encoded_local_paths() -> None:
    encoded_path = "C%3A%5CUsers%5Calice%5C.iac-code%5Cprojects%5Cdemo%5Ctemplate.yaml"

    artifact = sanitize_public_artifact_data(
        {
            "filename": encoded_path,
            "metadata": {"label": encoded_path},
        }
    )

    assert artifact["filename"] == "template.yaml"
    assert artifact["metadata"]["label"] == "[PATH]"
    rendered = str(artifact)
    assert "%5CUsers" not in rendered
    assert ".iac-code" not in rendered


@pytest.mark.parametrize("suffix", [".", ":", "!", "?"])
def test_sanitize_public_artifact_text_preserves_valid_opaque_uri_with_trailing_punctuation(suffix) -> None:
    uri = "iac-code-artifact://artifact-1/template.yaml"

    assert sanitize_public_artifact_text(f"see {uri}{suffix}") == f"see {uri}{suffix}"


def test_sanitize_public_artifact_text_preserves_valid_opaque_uri_before_prose() -> None:
    uri = "iac-code-artifact://artifact-1/template.yaml"

    assert sanitize_public_artifact_text(f"see {uri} and next") == f"see {uri} and next"


def test_sanitize_public_artifact_text_decodes_percent_encoded_local_paths() -> None:
    encoded_path = "file%3A%2F%2F%2FUsers%2Falice%2F.iac-code%2Fprojects%2Fdemo%2Ftemplate.yaml"

    sanitized = sanitize_public_artifact_text(f"see {encoded_path}")

    assert sanitized == "see [PATH]"
    assert "%2FUsers" not in sanitized
    assert ".iac-code" not in sanitized


def test_sanitize_public_artifact_text_redacts_raw_file_uri_with_spaces() -> None:
    value = r"failed at file:///Users/Alice and Bob/.iac-code/projects/demo/template.yaml and next"

    assert sanitize_public_artifact_text(value) == "failed at [PATH]"


def test_sanitize_public_artifact_text_redacts_connector_words_inside_final_filename() -> None:
    value = "failed at file:///Users/Alice Smith/.iac-code/projects/demo/template from prod.yaml and next"

    assert sanitize_public_artifact_text(value) == "failed at [PATH]"


def test_sanitize_public_artifact_text_redacts_extensionless_filename_connector_tail() -> None:
    value = "failed at file:///Users/Alice Smith/.iac-code/projects/demo/template from prod and next"

    assert sanitize_public_artifact_text(value) == "failed at [PATH]"


def test_sanitize_public_artifact_payload_keys_are_case_insensitive() -> None:
    artifact = sanitize_public_artifact_data(
        {
            "filename": "result.txt",
            "Content": "secret content",
            "Raw": "secret raw",
            "Base64": "c2VjcmV0",
            "Path": r"C:\Users\alice\.iac-code\projects\demo\template.yaml",
            "metadata": {"label": "safe"},
        }
    )

    assert artifact == {"filename": "result.txt", "metadata": {"label": "safe"}}


def test_sanitize_public_tool_output_handles_artifacts_containers_and_sensitive_keys() -> None:
    output = sanitize_public_tool_output_data(
        {
            "artifacts": [
                {
                    "filename": "result.txt",
                    "Content": "secret content",
                    "Raw": "secret raw",
                    "Path": r"C:\Users\Alice and Bob\.iac-code\projects\demo\template.yaml",
                    "metadata": {"token": "plain-token"},
                }
            ],
            "api_key": "secret-key",
            "note": "stored at /Users/Alice and Bob/.iac-code/projects/demo/template.yaml\nnext",
        }
    )

    assert output == {
        "artifacts": [{"filename": "result.txt", "metadata": {"token": "[REDACTED]"}}],
        "api_key": "[REDACTED]",
        "note": "stored at [PATH]\nnext",
    }


def test_sanitize_public_tool_output_handles_root_artifact_payload_dicts() -> None:
    output = sanitize_public_tool_output_data(
        [
            {
                "filename": "template.yaml",
                "Content": "RAW-TEMPLATE-CONTENT",
                "Raw": "raw-secret",
                "Base64": "YmFzZTY0",
                "metadata": {"api_key": "plain-secret"},
            }
        ]
    )

    assert output == [{"filename": "template.yaml", "metadata": {"api_key": "[REDACTED]"}}]


def test_sanitize_public_tool_output_preserves_non_artifact_content_metadata() -> None:
    output = sanitize_public_tool_output_data({"content": "visible output", "metadata": {"label": "safe"}})

    assert output == {"content": "visible output", "metadata": {"label": "safe"}}


def test_sanitize_public_tool_output_preserves_non_artifact_content_urls() -> None:
    output = sanitize_public_tool_output_data(
        {
            "title": "ROS docs",
            "content": "visible output",
            "url": "https://example.test/doc",
            "source_url": "https://example.test/source",
        }
    )

    assert output == {
        "title": "ROS docs",
        "content": "visible output",
        "url": "https://example.test/doc",
        "source_url": "https://example.test/source",
    }
