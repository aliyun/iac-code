import base64
from pathlib import Path

from iac_code.mcp.output import convert_mcp_tool_result


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

    artifacts = result.metadata["mcp"]["artifacts"]
    assert len(artifacts) == 2
    artifact_paths = [Path(artifact["path"]) for artifact in artifacts]
    artifact_root = tmp_path / "config" / "tool-results" / "session-1" / "mcp"
    assert all(path.exists() for path in artifact_paths)
    assert all(str(path).startswith(str(artifact_root)) for path in artifact_paths)
    assert artifact_paths[0].read_bytes() == b"fake-png"
    assert artifact_paths[1].read_bytes() == b"resource-bytes"
    assert artifacts[1]["uri"] == "file:///tmp/archive.bin"


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
