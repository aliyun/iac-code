from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    RosResourceTypeDetail,
    build_resource_inventory_snapshot,
)
from iac_code.pipeline.engine.architecture_rule_candidates import (
    build_resource_type_decisions,
    extract_rule_candidates,
)


def _load_script_module():
    script_path = Path("scripts/rendering/analyze_ros_resource_architecture_rules.py")
    spec = importlib.util.spec_from_file_location("analyze_ros_resource_architecture_rules", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot():
    repo = ArchitectureMetaRepository.from_raw(
        categories=[{"CategoryCode": "network", "ProductCodes": ["ecs"]}],
        products=[{"ProductCode": "ecs", "Name": {"en": "ECS", "zh": "云服务器"}, "RelevantCodes": {"ROS": "ECS"}}],
        config=[
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::VPC"},
                "ProductCode": "ecs",
                "Name": {"en": "VPC", "zh": "专有网络 VPC"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::RunCommand"},
                "ProductCode": "ecs",
                "Name": {"en": "Run Command", "zh": "执行命令"},
                "Properties": [
                    {
                        "ROS": "InstanceIds",
                        "RelatedTo": [{"ResourceType": "ROS/ALIYUN::ECS::Instance"}],
                    }
                ],
            },
        ],
    )
    return build_resource_inventory_snapshot(
        api_resource_types=["ALIYUN::ECS::VPC", "ALIYUN::ECS::RunCommand"],
        details_by_type={
            "ALIYUN::ECS::RunCommand": RosResourceTypeDetail(
                resource_type="ALIYUN::ECS::RunCommand",
                entity_type="Resource",
                provider="ROS",
                properties={"CommandContent": {"Description": "The command content."}},
                attributes={},
                description="Runs a Cloud Assistant command on ECS instances.",
            )
        },
        meta_repository=repo,
        fetched_at="2026-06-26T00:00:00Z",
    )


def test_write_analysis_outputs_creates_json_and_markdown(tmp_path: Path) -> None:
    module = _load_script_module()
    snapshot = _snapshot()
    candidates = extract_rule_candidates(snapshot)
    decisions = build_resource_type_decisions(snapshot, candidates)
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"

    module.write_analysis_outputs(
        snapshot=snapshot,
        candidates=candidates,
        decisions=decisions,
        json_out=json_out,
        markdown_out=markdown_out,
    )

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["summary"]["api_resource_types"] == 2
    assert payload["summary"]["resource_facts"] == 2
    assert payload["resource_facts"]
    assert payload["rule_signals"]
    assert "decisions" not in payload
    assert "candidates" not in payload

    markdown = markdown_out.read_text(encoding="utf-8")
    assert "# ROS 架构图 Resource Facts / Rule Signals" in markdown
    assert "`ALIYUN::ECS::RunCommand`" in markdown
