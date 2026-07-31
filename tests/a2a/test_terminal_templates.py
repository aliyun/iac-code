from __future__ import annotations

import hashlib

import pytest

from iac_code.a2a.terminal_templates import A2ATerminalTemplateCollector


@pytest.mark.asyncio
async def test_terminal_template_collector_returns_supported_templates_in_path_order(
    tmp_path,
) -> None:
    terraform = 'resource "alicloud_vpc" "main" {\n  vpc_name = "example"\n}\n'
    ros_yaml = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  Vpc:\n    Type: ALIYUN::ECS::VPC\n"
    (tmp_path / "b.tf").write_text(terraform, encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.yaml").write_text(ros_yaml, encoding="utf-8")
    (tmp_path / "notes.yaml").write_text("title: notes\n", encoding="utf-8")

    templates = await A2ATerminalTemplateCollector().collect(tmp_path)

    assert templates == [
        {
            "filePath": "b.tf",
            "content": terraform,
            "format": "terraform",
            "contentSha256": hashlib.sha256(terraform.encode()).hexdigest(),
        },
        {
            "filePath": "nested/a.yaml",
            "content": ros_yaml,
            "format": "yaml",
            "contentSha256": hashlib.sha256(ros_yaml.encode()).hexdigest(),
        },
    ]


@pytest.mark.asyncio
async def test_terminal_template_collector_ignores_symlinks_outside_workspace(
    tmp_path,
) -> None:
    outside = tmp_path.parent / "outside-template.yaml"
    outside.write_text("Resources: {}\n", encoding="utf-8")
    link = tmp_path / "linked.yaml"
    link.symlink_to(outside)

    templates = await A2ATerminalTemplateCollector().collect(tmp_path)

    assert templates == []


@pytest.mark.asyncio
async def test_terminal_template_collector_enforces_total_payload_limit(
    tmp_path,
) -> None:
    (tmp_path / "a.yaml").write_text("Resources: {}\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("Resources:\n  B: {}\n", encoding="utf-8")
    collector = A2ATerminalTemplateCollector(max_total_bytes=15)

    templates = await collector.collect(tmp_path)

    assert [template["filePath"] for template in templates] == ["a.yaml"]
