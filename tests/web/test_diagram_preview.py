from __future__ import annotations

import json

from starlette.testclient import TestClient

from iac_code.web.app import MAX_DIAGRAM_PREVIEW_BYTES, create_app

_ROS_TEMPLATE = """ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
  Ecs:
    Type: ALIYUN::ECS::Instance
    Properties:
      VpcId:
        Ref: Vpc
"""


def _client(tmp_path, monkeypatch, *, token_mode: bool = False) -> TestClient:
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    return TestClient(create_app(token_mode=token_mode))


def test_diagram_preview_page_uses_saved_theme_renderer_and_token_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("iac_code.web.app.resolve_ui_language", lambda override: "en")
    client = _client(tmp_path, monkeypatch, token_mode=True)
    with client:
        assert client.put("/api/settings/appearance", json={"theme": "midnight"}).status_code == 200
        assert client.put("/api/settings/architecture-diagram", json={"renderer": "mermaid"}).status_code == 200
        response = client.get("/diagram-preview")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert '<html lang="en" data-theme="midnight">' in response.text
    assert 'data-architecture-diagram-renderer="mermaid"' in response.text
    assert 'data-token-mode="true"' in response.text
    assert "/static/js/diagram_preview.js?v=diagram-preview-v7" in response.text


def test_diagram_preview_serves_the_shared_30_template_corpus(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        response = client.get("/static/diagram-preview-corpus.json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["templateRepositoryCommit"] == "b2d8085990d471094a2de7f90b131992f6ba0795"
    assert len(payload["templates"]) == 30
    assert len(set(payload["templates"])) == 30


def test_diagram_preview_api_renders_yaml_and_json_without_persisting(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    json_template = json.dumps(
        {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Resources": {"Vpc": {"Type": "ALIYUN::ECS::VPC", "Properties": {}}},
        }
    )
    with client:
        yaml_response = client.post(
            "/api/diagram-preview",
            json={"filename": "nested/template.yaml", "content": _ROS_TEMPLATE},
        )
        json_response = client.post(
            "/api/diagram-preview",
            json={"filename": "template.json", "content": json_template},
        )

    for response in (yaml_response, json_response):
        assert response.status_code == 200
        payload = response.json()
        assert payload["format"] == "ros"
        assert payload["views"]
        assert payload["views"][0]["graph"]["version"] == 1
        assert payload["views"][0]["mermaidSource"].startswith("graph TD")
    assert yaml_response.json()["filename"] == "template.yaml"


def test_di_preview_api_rejects_unsupported_invalid_and_oversized_files(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        unsupported = client.post(
            "/api/diagram-preview",
            json={"filename": "main.tf", "content": 'resource "alicloud_vpc" "main" {}'},
        )
        invalid = client.post(
            "/api/diagram-preview",
            json={"filename": "template.yaml", "content": "Resources:\n  broken: ["},
        )
        empty = client.post(
            "/api/diagram-preview",
            json={"filename": "template.yaml", "content": "ROSTemplateFormatVersion: '2015-09-01'\n"},
        )
        oversized = client.post(
            "/api/diagram-preview",
            json={"filename": "template.yaml", "content": "x" * (MAX_DIAGRAM_PREVIEW_BYTES + 1)},
        )

    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "unsupported_template_format"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "template_parse_failed"
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "template_has_no_resources"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "template_too_large"
