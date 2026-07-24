from pathlib import Path

import pytest

from iac_code.a2a.artifacts import (
    A2AArtifactStore,
    UnsafeArtifactNameError,
    artifact_store_for_session,
    sanitize_public_artifact_data,
    sanitize_public_artifact_text,
    sanitize_public_tool_output_data,
)
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SESSION_LAYOUT_VERSION_V2, SessionMetadata, write_session_metadata


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


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


def test_artifact_store_for_session_uses_session_a2a_artifacts_dir(tmp_path) -> None:
    session_dir = tmp_path / "projects" / "p" / "session-1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="session-1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )

    store = artifact_store_for_session(session_dir)

    assert store.root == session_dir / "a2a" / "artifacts"


def test_artifact_store_for_session_rejects_symlink_artifacts_dir(tmp_path) -> None:
    session_dir = tmp_path / "projects" / "p" / "session-1"
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id="session-1", cwd="/repo", layout_version=SESSION_LAYOUT_VERSION_V2),
    )
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (session_dir / "a2a").mkdir()
    _symlink_or_skip(outside, session_dir / "a2a" / "artifacts", target_is_directory=True)

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        artifact_store_for_session(session_dir)

    assert list(outside.iterdir()) == []


def test_artifact_store_for_session_rejects_legacy_session_dir(tmp_path) -> None:
    session_dir = tmp_path / "projects" / "p" / "session-1"
    session_dir.mkdir(parents=True)

    with pytest.raises(UnsupportedSessionLayoutError):
        artifact_store_for_session(session_dir)


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


def test_sanitize_public_artifact_text_relativizes_file_uri_under_public_root() -> None:
    roots = [{"path": "/Users/alice/project", "label": "."}]

    sanitized = sanitize_public_artifact_text(
        "see file:///Users/alice/project/output/template.yaml",
        public_path_roots=roots,
    )

    assert sanitized == "see ./output/template.yaml"
    assert "/Users/alice" not in sanitized


def test_sanitize_public_artifact_text_relativizes_localhost_file_uri_under_public_root() -> None:
    roots = [{"path": "/Users/alice/project", "label": "."}]

    sanitized = sanitize_public_artifact_text(
        "see file://localhost/Users/alice/project/output/template.yaml",
        public_path_roots=roots,
    )

    assert sanitized == "see ./output/template.yaml"
    assert "/Users/alice" not in sanitized


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


def test_sanitize_public_artifact_data_redacts_file_content_metadata() -> None:
    artifact = sanitize_public_artifact_data(
        {
            "filename": "template.yaml",
            "metadata": {
                "file_content": "SECRET_TEMPLATE",
                "fileContent": "SECRET_TEMPLATE_CAMEL",
                "nested": {"file_content": "NESTED_SECRET_TEMPLATE"},
            },
        }
    )

    assert artifact == {
        "filename": "template.yaml",
        "metadata": {
            "file_content": "[REDACTED]",
            "fileContent": "[REDACTED]",
            "nested": {"file_content": "[REDACTED]"},
        },
    }
    assert "SECRET_TEMPLATE" not in str(artifact)


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


def test_sanitize_public_tool_output_redacts_artifact_file_content_metadata() -> None:
    output = sanitize_public_tool_output_data(
        {
            "artifact": {
                "filename": "template.yaml",
                "content": "artifact payload should be omitted",
                "metadata": {"file_content": "SECRET_TEMPLATE"},
            }
        }
    )

    assert output == {
        "artifact": {
            "filename": "template.yaml",
            "metadata": {"file_content": "[REDACTED]"},
        }
    }
    assert "SECRET_TEMPLATE" not in str(output)


def test_sanitize_public_tool_output_relativizes_paths_under_public_roots() -> None:
    output = sanitize_public_tool_output_data(
        "STDOUT:\n"
        "/Users/alice/project/src/app.py:12\n"
        "/Users/alice/.iac-code/tool-results/session-1/result.txt\n"
        "/Volumes/shared/templates/base.yaml\n"
        "/Users/alice/private/secret.txt\n"
        "Exit code: 0",
        public_path_roots=[
            {"path": "/Users/alice/project", "label": "."},
            {"path": "/Users/alice/.iac-code", "label": "$IAC_CODE_CONFIG_DIR"},
            {"path": "/Volumes/shared/templates", "label": "[trusted]"},
        ],
    )

    assert output == (
        "STDOUT:\n"
        "./src/app.py:12\n"
        "$IAC_CODE_CONFIG_DIR/tool-results/session-1/result.txt\n"
        "[trusted]/base.yaml\n"
        "[PATH]\n"
        "Exit code: 0"
    )


def test_sanitize_public_tool_output_relativizes_path_keys_under_public_roots() -> None:
    output = sanitize_public_tool_output_data(
        {
            "/Users/alice/project/src/app.py": "workspace",
            "/Users/alice/.iac-code/tool-results/session-1/result.txt": "config",
            "/Volumes/shared/templates/base.yaml": "trusted",
            "/opt/private/config.txt": "outside",
            "nested": {"/Users/alice/project/output.yaml": "nested"},
        },
        public_path_roots=[
            {"path": "/Users/alice/project", "label": "."},
            {"path": "/Users/alice/.iac-code", "label": "$IAC_CODE_CONFIG_DIR"},
            {"path": "/Volumes/shared/templates", "label": "[trusted]"},
        ],
    )

    assert output == {
        "./src/app.py": "workspace",
        "$IAC_CODE_CONFIG_DIR/tool-results/session-1/result.txt": "config",
        "[trusted]/base.yaml": "trusted",
        "[PATH]": "outside",
        "nested": {"./output.yaml": "nested"},
    }


def test_sanitize_public_tool_output_redacts_sensitive_keys_that_also_contain_paths() -> None:
    output = sanitize_public_tool_output_data(
        {
            "password_file /Users/alice/project/config.txt": "super-secret",
            "/Users/alice/project/config.txt password_file": "suffix-secret",
            "file:///Users/alice/project/config-uri.txt password_file": "uri-suffix-secret",
        },
        public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
    )

    assert output == {
        "password_file ./config.txt": "[REDACTED]",
        "./config.txt password_file": "[REDACTED]",
        "./config-uri.txt password_file": "[REDACTED]",
    }


def test_sanitize_public_artifact_data_relativizes_path_keys_under_public_roots() -> None:
    output = sanitize_public_artifact_data(
        {
            "filename": "result.txt",
            "metadata": {
                "/Users/alice/project/template.yaml": "workspace",
                "/Volumes/shared/templates/base.yaml": "trusted",
            },
        },
        public_path_roots=[
            {"path": "/Users/alice/project", "label": "."},
            {"path": "/Volumes/shared/templates", "label": "[trusted]"},
        ],
    )

    assert output == {
        "filename": "result.txt",
        "metadata": {
            "./template.yaml": "workspace",
            "[trusted]/base.yaml": "trusted",
        },
    }


def test_sanitize_public_artifact_data_redacts_sensitive_keys_that_also_contain_paths() -> None:
    output = sanitize_public_artifact_data(
        {
            "metadata": {
                "password_file /Users/alice/project/config.txt": "super-secret",
                "/Users/alice/project/config.txt password_file": "suffix-secret",
            }
        },
        public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
    )

    assert output == {
        "metadata": {
            "password_file ./config.txt": "[REDACTED]",
            "./config.txt password_file": "[REDACTED]",
        }
    }


def test_sanitize_public_tool_output_redacts_unmatched_absolute_paths_without_touching_urls() -> None:
    output = sanitize_public_tool_output_data(
        "files:\n"
        "/opt/private/secret.txt\n"
        "/mnt/data/foo.txt\n"
        "/Volumes/shared/secret.txt\n"
        "https://example.com/docs/template.yaml\n"
        "path=/srv/app/config.yaml",
        public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
    )

    assert output == ("files:\n[PATH]\n[PATH]\n[PATH]\nhttps://example.com/docs/template.yaml\npath=[PATH]")


def test_sanitize_public_tool_output_preserves_json_unicode_escapes() -> None:
    output = sanitize_public_tool_output_data(
        r'{"Label": "{\"en\": \"VPC\", \"zh-cn\": \"\\u4e13\\u6709\\u7f51\\u7edc\"}"}',
        public_path_roots=[{"path": "/Users/alice/project", "label": "."}],
    )

    assert output == r'{"Label": "{\"en\": \"VPC\", \"zh-cn\": \"\\u4e13\\u6709\\u7f51\\u7edc\"}"}'


def test_sanitize_public_tool_output_uses_shortest_matching_public_root() -> None:
    output = sanitize_public_tool_output_data(
        "/repo/subdir/file.yaml",
        public_path_roots=[
            {"path": "/repo", "label": "."},
            {"path": "/repo/subdir", "label": "[trusted]"},
        ],
    )

    assert output == "./subdir/file.yaml"


def test_sanitize_public_tool_output_relativizes_windows_paths_under_public_roots() -> None:
    output = sanitize_public_tool_output_data(
        r"C:\Users\alice\project\src\app.py:12",
        public_path_roots=[{"path": r"C:\Users\alice\project", "label": "."}],
    )

    assert output == "./src/app.py:12"
    assert "C:\\Users\\alice" not in output


def test_sanitize_public_tool_output_relativizes_unc_paths_under_public_roots() -> None:
    output = sanitize_public_tool_output_data(
        r"\\server\share\dir\file.txt",
        public_path_roots=[{"path": r"\\server\share", "label": "[trusted]"}],
    )

    assert output == "[trusted]/dir/file.txt"
    assert "\\\\server\\share" not in output


def test_sanitize_public_tool_output_relativizes_unc_paths_with_unicode_like_server_names() -> None:
    output = sanitize_public_tool_output_data(
        r"\\u1234\share\dir\file.txt",
        public_path_roots=[{"path": r"\\u1234\share", "label": "[trusted]"}],
    )

    assert output == "[trusted]/dir/file.txt"
    assert r"\\u1234\share" not in output


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


def test_sanitize_public_artifact_text_keeps_file_uri_when_all_redaction_suppressed() -> None:
    from iac_code.utils.public_errors import suppress_all_redaction

    value = "see file:///Users/alice/.iac-code/demo/t.yaml, next"
    assert sanitize_public_artifact_text(value) == "see [PATH], next"
    with suppress_all_redaction():
        assert sanitize_public_artifact_text(value) == value


def test_sanitize_public_artifact_data_keeps_everything_raw_when_suppressed() -> None:
    from iac_code.utils.public_errors import suppress_all_redaction

    payload = {"note": "file:///Users/alice/.iac-code/demo/t.yaml", "password": "p@ss"}
    assert sanitize_public_artifact_data(payload) == {"note": "[PATH]", "password": "[REDACTED]"}
    with suppress_all_redaction():
        assert sanitize_public_artifact_data(payload) == {
            "note": "file:///Users/alice/.iac-code/demo/t.yaml",
            "password": "p@ss",
        }
