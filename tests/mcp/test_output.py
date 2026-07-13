import base64
from pathlib import Path
from typing import Any, cast

import pytest

import iac_code.mcp.output as mcp_output
from iac_code.agent.message import Message, ToolResultBlock
from iac_code.mcp.output import convert_mcp_tool_result
from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SessionMetadata, write_session_metadata
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolResult


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


def _mcp_artifacts(result: ToolResult) -> list[dict[str, Any]]:
    assert result.metadata is not None
    mcp_metadata = result.metadata["mcp"]
    assert isinstance(mcp_metadata, dict)
    artifacts = mcp_metadata["artifacts"]
    assert isinstance(artifacts, list)
    return cast(list[dict[str, Any]], artifacts)


def test_convert_mcp_result_includes_text_structured_content_and_meta(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = convert_mcp_tool_result(
        {
            "content": [{"type": "text", "text": "created VPC template"}],
            "structuredContent": {"template": {"ROSTemplateFormatVersion": "2015-09-01"}},
            "_meta": {"traceId": "trace-1"},
        },
        server_name="ros",
        tool_name="generate_template",
        session_id="session-1",
    )

    assert result.is_error is False
    assert "created VPC template" in result.content
    assert '"ROSTemplateFormatVersion": "2015-09-01"' in result.content
    assert result.metadata == {
        "mcp": {
            "server_name": "ros",
            "tool_name": "generate_template",
            "is_error": False,
            "meta": {"traceId": "trace-1"},
            "artifacts": [],
        }
    }


def test_convert_mcp_result_includes_resource_text_and_resource_links(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = convert_mcp_tool_result(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "skill://ros/vpc",
                        "mimeType": "text/markdown",
                        "text": "# VPC\nUse vSwitches deliberately.",
                    },
                },
                {
                    "type": "resource_link",
                    "uri": "file:///tmp/template.yml",
                    "name": "template.yml",
                    "mimeType": "text/yaml",
                },
            ]
        },
        server_name="ros",
        tool_name="read_context",
        session_id="session-1",
    )

    assert "Resource from MCP server 'ros'" in result.content
    assert "URI: skill://ros/vpc" in result.content
    assert "# VPC" in result.content
    assert "Resource link: template.yml" in result.content
    assert "file:///tmp/template.yml" in result.content
    assert "text/yaml" in result.content


def test_convert_mcp_result_stores_large_text_without_inlining(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 32)
    large_text = "large-result-line\n" * 5

    result = convert_mcp_tool_result(
        {"content": [{"type": "text", "text": large_text}]},
        server_name="ros/server",
        tool_name="generate template",
        session_id="session-1",
    )

    assert large_text not in result.content
    assert "Saved large MCP text output" in result.content
    assert "Read the full output from" in result.content

    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    artifact_path = Path(artifact["path"])
    assert artifact_path.suffix == ".txt"
    assert artifact_path.parent == tmp_path / "config" / "tool-results" / "session-1" / "mcp" / "ros-server" / (
        "generate-template"
    )
    assert artifact_path.read_text(encoding="utf-8") == large_text
    assert artifact["kind"] == "text"
    assert artifact["mime_type"] == "text/plain"
    assert artifact["size"] == len(large_text.encode("utf-8"))
    assert str(artifact_path) in result.content


def test_convert_mcp_result_stores_large_resource_text_with_mime_extension(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 8)
    markdown_text = "# VPC\n\n" + ("Use private subnets.\n" * 3)

    result = convert_mcp_tool_result(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "skill://ros/vpc.md",
                        "mimeType": "text/markdown",
                        "text": markdown_text,
                    },
                }
            ]
        },
        server_name="ros",
        tool_name="read_context",
        session_id="session-1",
        session_dir=tmp_path / "sessions" / "session-1",
    )

    assert markdown_text not in result.content
    assert "skill://ros/vpc.md" in result.content

    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    artifact_path = Path(artifact["path"])
    assert artifact_path.suffix == ".md"
    assert artifact_path.read_text(encoding="utf-8") == markdown_text
    assert artifact["kind"] == "resource"
    assert artifact["mime_type"] == "text/markdown"
    assert artifact["uri"] == "skill://ros/vpc.md"


def test_convert_mcp_result_stores_large_json_text_with_json_extension(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 8)
    json_text = '{"Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC"}}}'

    result = convert_mcp_tool_result(
        {"content": [{"type": "text", "text": json_text, "mimeType": "application/json"}]},
        server_name="ros",
        tool_name="generate_template",
        session_id="session-1",
    )

    assert json_text not in result.content
    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact_path = Path(artifacts[0]["path"])
    assert artifact_path.suffix == ".json"
    assert artifact_path.read_text(encoding="utf-8") == json_text
    assert artifacts[0]["mime_type"] == "application/json"


def test_convert_mcp_result_stores_large_structured_content_as_json_artifact(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 32)
    payload = "STRUCTURED_START_" + ("x" * 64) + "_STRUCTURED_END"

    result = convert_mcp_tool_result(
        {"structuredContent": {"payload": payload}},
        server_name="ros",
        tool_name="generate_template",
        session_id="session-1",
    )

    assert payload not in result.content
    assert "Structured content:" in result.content
    assert "Saved large MCP text output" in result.content

    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    artifact_path = Path(artifact["path"])
    assert artifact_path.suffix == ".json"
    assert artifact_path.read_text(encoding="utf-8") == '{\n  "payload": "' + payload + '"\n}'
    assert artifact["kind"] == "structured-content"
    assert artifact["mime_type"] == "application/json"
    assert artifact["size"] == len(artifact_path.read_bytes())
    assert artifact["chars"] == len(artifact_path.read_text(encoding="utf-8"))


def test_large_structured_content_session_jsonl_stores_artifact_reference_not_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 32)
    payload = "STRUCTURED_JSONL_START_" + ("x" * 64) + "_STRUCTURED_JSONL_END"
    cwd = str(tmp_path / "project")
    session_id = "session-1"
    result = convert_mcp_tool_result(
        {"structuredContent": {"payload": payload}},
        server_name="ros",
        tool_name="generate_template",
        session_id=session_id,
    )
    storage = SessionStorage(projects_dir=tmp_path / "projects")

    storage.append(
        cwd,
        session_id,
        Message(role="user", content=[ToolResultBlock(tool_use_id="toolu_structured", content=result.content)]),
    )

    session_jsonl = storage.session_path(cwd, session_id).read_text(encoding="utf-8")
    assert payload not in session_jsonl
    artifacts = _mcp_artifacts(result)
    assert Path(artifacts[0]["path"]).read_text(encoding="utf-8").endswith(payload + '"\n}')
    assert "Read the full output from" in session_jsonl


def test_structured_content_uses_byte_threshold_before_inlining(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 10_000)
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_BYTES", 64)
    payload = "结构化内容" * 8

    result = convert_mcp_tool_result(
        {"structuredContent": {"payload": payload}},
        server_name="ros",
        tool_name="generate_template",
        session_id="session-1",
    )

    assert payload not in result.content
    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact_path = Path(artifacts[0]["path"])
    assert artifact_path.suffix == ".json"
    assert payload in artifact_path.read_text(encoding="utf-8")


def test_convert_mcp_result_stores_large_yaml_text_with_txt_extension(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(mcp_output, "MAX_INLINE_TEXT_CHARS", 8)
    yaml_text = "Resources:\n  Vpc:\n    Type: ALIYUN::ECS::VPC\n"

    result = convert_mcp_tool_result(
        {"content": [{"type": "text", "text": yaml_text, "mimeType": "text/yaml"}]},
        server_name="ros",
        tool_name="generate_template",
        session_id="session-1",
    )

    assert yaml_text not in result.content
    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact_path = Path(artifacts[0]["path"])
    assert artifact_path.suffix == ".txt"
    assert artifact_path.read_text(encoding="utf-8") == yaml_text
    assert artifacts[0]["mime_type"] == "text/yaml"


def test_convert_mcp_result_stores_binary_content_without_exposing_base64(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    image_data = base64.b64encode(b"fake-png").decode("ascii")
    blob_data = base64.b64encode(b"resource-bytes").decode("ascii")

    result = convert_mcp_tool_result(
        {
            "content": [
                {"type": "image", "data": image_data, "mimeType": "image/png"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/archive.bin",
                        "mimeType": "application/octet-stream",
                        "blob": blob_data,
                    },
                },
            ]
        },
        server_name="ros",
        tool_name="render",
        session_id="session-1",
    )

    assert image_data not in result.content
    assert blob_data not in result.content
    assert str(tmp_path) not in result.content
    assert "Saved image/png artifact" in result.content
    assert "Saved application/octet-stream artifact" in result.content

    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 2
    artifact_paths = [Path(artifact["path"]) for artifact in artifacts]
    artifact_root = tmp_path / "config" / "tool-results" / "session-1" / "mcp"
    assert all(path.exists() for path in artifact_paths)
    assert all(str(path).startswith(str(artifact_root)) for path in artifact_paths)
    assert artifact_paths[0].read_bytes() == b"fake-png"
    assert artifact_paths[1].read_bytes() == b"resource-bytes"
    assert artifacts[1]["uri"] == "file:///tmp/archive.bin"


def test_convert_mcp_result_stores_binary_resource_with_common_mime_extensions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    pdf_data = base64.b64encode(b"%PDF-1.7\n").decode("ascii")
    mp4_data = base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode("ascii")

    result = convert_mcp_tool_result(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/report.pdf",
                        "mimeType": "application/pdf",
                        "blob": pdf_data,
                    },
                },
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/demo.mp4",
                        "mimeType": "video/mp4",
                        "blob": mp4_data,
                    },
                },
            ]
        },
        server_name="ros",
        tool_name="read_resource",
        session_id="session-1",
    )

    artifacts = _mcp_artifacts(result)
    artifact_paths = [Path(artifact["path"]) for artifact in artifacts]
    assert [path.suffix for path in artifact_paths] == [".pdf", ".mp4"]
    assert [artifact["mime_type"] for artifact in artifacts] == ["application/pdf", "video/mp4"]
    assert artifact_paths[0].read_bytes() == b"%PDF-1.7\n"
    assert artifact_paths[1].read_bytes() == b"\x00\x00\x00\x18ftypmp42"


def test_convert_mcp_result_stores_binary_content_under_session_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    image_data = base64.b64encode(b"session-png").decode("ascii")
    session_dir = tmp_path / "sessions" / "session-1"

    result = convert_mcp_tool_result(
        {"content": [{"type": "image", "data": image_data, "mimeType": "image/png"}]},
        server_name="ros/server",
        tool_name="render template",
        session_id="session-1",
        session_dir=session_dir,
    )

    artifacts = _mcp_artifacts(result)
    assert len(artifacts) == 1
    artifact_path = Path(artifacts[0]["path"])
    assert artifact_path.parent == session_dir / "tool-results" / "mcp" / "ros-server" / "render-template"
    assert artifact_path.read_bytes() == b"session-png"
    assert not (tmp_path / "config" / "tool-results" / "session-1").exists()


def test_convert_mcp_result_rejects_symlink_session_tool_results(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "session-1"
    session_dir = tmp_path / "sessions" / session_id
    write_session_metadata(session_dir, SessionMetadata(session_id=session_id, cwd=str(tmp_path), layout_version=2))
    outside = tmp_path / "outside-tool-results"
    outside.mkdir()
    _symlink_or_skip(outside, session_dir / "tool-results", target_is_directory=True)
    image_data = base64.b64encode(b"session-png").decode("ascii")

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        convert_mcp_tool_result(
            {"content": [{"type": "image", "data": image_data, "mimeType": "image/png"}]},
            server_name="ros",
            tool_name="render",
            session_id=session_id,
            session_dir=session_dir,
        )

    assert list(outside.iterdir()) == []


def test_convert_mcp_result_rejects_future_session_layout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    session_id = "future-session"
    session_dir = tmp_path / "sessions" / session_id
    write_session_metadata(
        session_dir,
        SessionMetadata(session_id=session_id, cwd=str(tmp_path), layout_version=99),
    )
    image_data = base64.b64encode(b"future-png").decode("ascii")

    with pytest.raises(UnsupportedSessionLayoutError):
        convert_mcp_tool_result(
            {"content": [{"type": "image", "data": image_data, "mimeType": "image/png"}]},
            server_name="ros",
            tool_name="render",
            session_id=session_id,
            session_dir=session_dir,
        )

    assert not (tmp_path / "config" / "tool-results" / session_id).exists()
    assert not (session_dir / "tool-results").exists()


def test_convert_mcp_is_error_maps_to_tool_result_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))

    result = convert_mcp_tool_result(
        {"content": [{"type": "text", "text": "remote tool failed"}], "isError": True},
        server_name="ros",
        tool_name="apply",
        session_id="session-1",
    )

    assert result.is_error is True
    assert "remote tool failed" in result.content
