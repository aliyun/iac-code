from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_script_module():
    script_path = Path("scripts/rendering/preview_template_architecture_llm.py")
    spec = importlib.util.spec_from_file_location("preview_template_architecture_llm", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_semantic_plan_json_accepts_fenced_json():
    module = _load_script_module()

    result = module.extract_semantic_plan_json(
        """```json
{"node_labels":[{"id":"A","label":"App","confidence":"high"}],"edges":[{"from":"A","to":"B","kind":"traffic","label":"traffic","confidence":"high"}]}
```"""
    )

    assert result == {
        "node_labels": [{"id": "A", "label": "App", "confidence": "high"}],
        "edges": [{"from": "A", "to": "B", "kind": "traffic", "label": "traffic", "confidence": "high"}],
    }


def test_extract_semantic_plan_json_rejects_missing_json_object():
    module = _load_script_module()

    with pytest.raises(ValueError, match="JSON object"):
        module.extract_semantic_plan_json("no json here")


def test_try_extract_semantic_plan_json_returns_parse_error():
    module = _load_script_module()

    value, error = module.try_extract_semantic_plan_json('{"node_labels": [{"id": "A"')

    assert value is None
    assert error is not None
    assert "JSON" in error or "delimiter" in error


def test_build_semantic_plan_user_prompt_contains_fact_bundle():
    module = _load_script_module()

    prompt = module.build_semantic_plan_user_prompt(
        {
            "target_language": {"code": "zh", "name": "Chinese"},
            "visible_nodes": [{"id": "ECS"}],
            "llm_semantic_plan_schema": {
                "node_labels": {"max_label_chars": 32},
                "edges": {"max_label_chars": 18},
            },
        }
    )

    assert "visible_nodes" in prompt
    assert "target_language" in prompt
    assert "node_labels" in prompt
    assert "max_label_chars" in prompt
    assert "Return JSON only" in prompt


def test_build_llm_architecture_context_slims_facts_and_adds_scaffold():
    module = _load_script_module()

    architecture_context = {
        "template_summary": {
            "description": "在现有VPC下，创建Kafka集群，包含管理节点与弹性伸缩节点。",
            "descriptions": {"zh-cn": "在现有VPC下，创建Kafka集群。", "en": "Deploy a Kafka cluster."},
        },
        "target_language": {"code": "zh", "name": "Chinese"},
        "visible_nodes": [{"id": f"Node{i}", "type": "ALIYUN::ECS::Instance"} for i in range(35)],
        "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
        "containment": [{"resource": "Node1", "container": "Vpc"}],
        "visible_edges": [{"from": "Node1", "to": "Node2"}],
        "explicit_relations": [{"source": "Node1", "target": "Node2"}],
        "property_references": [{"source": f"Node{i}", "target": f"Node{i + 1}"} for i in range(80)],
        "all_property_references": [{"source": f"Node{i}", "target": f"Node{i + 1}"} for i in range(80)],
        "node_label_hints": [{"id": "Node1", "hints": {"Name": "APP01"}}],
        "orchestration_actions": [{"source": "Node1", "target": "Node2"}],
        "outputs": [{"name": "Endpoint"}],
        "llm_semantic_plan_schema": {"node_labels": {"max_label_chars": 32}},
        "semantic_plan": {"accepted_edges": [{"from": "Old", "to": "State"}]},
        "debug_dump": "x" * 1000,
    }

    slim = module.build_llm_architecture_context(architecture_context)

    assert "debug_dump" not in slim
    assert "semantic_plan" not in slim
    assert slim["template_summary"] == architecture_context["template_summary"]
    assert slim["visible_nodes"] == architecture_context["visible_nodes"]
    assert len(slim["property_references"]) < len(architecture_context["property_references"])
    assert len(slim["all_property_references"]) < len(architecture_context["all_property_references"])
    assert slim["truncation_summary"]["property_references"] == {
        "included_count": 48,
        "total_count": 80,
        "omitted_count": 32,
        "truncated": True,
    }
    assert slim["truncation_summary"]["all_property_references"] == {
        "included_count": 64,
        "total_count": 80,
        "omitted_count": 16,
        "truncated": True,
    }
    assert slim["semantic_plan_scaffold"]["views"][0]["id"] == "overview"
    assert len(json.dumps(slim, ensure_ascii=False)) < len(json.dumps(architecture_context, ensure_ascii=False))


def test_build_llm_architecture_context_keeps_network_attachments():
    module = _load_script_module()

    network_attachments = [
        {
            "id": "CenVpcAttachment",
            "type": "ALIYUN::CEN::CenInstanceAttachment",
            "network": "CEN",
            "cen": "CenInstance",
            "child_instance_type": "VPC",
            "child_instance_id": "Vpc",
            "child_resource": "Vpc",
            "child_resource_type": "ALIYUN::ECS::VPC",
        }
    ]
    slim = module.build_llm_architecture_context(
        {
            "target_language": {"code": "zh", "name": "Chinese"},
            "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"},
            ],
            "containment": [{"resource": "layer_CenInstance_Config", "container": "CenInstance"}],
            "network_attachments": network_attachments,
        }
    )

    assert slim["network_attachments"] == network_attachments


def test_build_llm_architecture_context_keeps_kubernetes_application_summary():
    module = _load_script_module()

    kubernetes_applications = [
        {
            "cluster": "Ack",
            "source": "AppHpa",
            "kind": "HorizontalPodAutoscaler",
            "name": "tea-hpa",
            "label": "HPA弹性伸缩",
            "template_refs": ["SlsProject"],
        }
    ]
    slim = module.build_llm_architecture_context(
        {
            "target_language": {"code": "zh", "name": "Chinese"},
            "visible_nodes": [
                {"id": "AckHpaAutoscaling", "type": "CONCEPT::ACK::HpaAutoscaling"},
                {"id": "SlsProject", "type": "ALIYUN::SLS::Project"},
            ],
            "kubernetes_applications": kubernetes_applications,
        }
    )

    assert slim["kubernetes_applications"] == kubernetes_applications


def test_build_llm_architecture_context_recommends_ack_application_detail_view():
    module = _load_script_module()

    slim = module.build_llm_architecture_context(
        {
            "target_language": {"code": "zh", "name": "Chinese"},
            "visible_nodes": [
                {"id": "AckApplicationWorkload", "type": "CONCEPT::ACK::ApplicationWorkload"},
                {"id": "AckServiceExposure", "type": "CONCEPT::ACK::ServiceExposure"},
                {"id": "AckHpaAutoscaling", "type": "CONCEPT::ACK::HpaAutoscaling"},
            ],
            "kubernetes_applications": [{"cluster": "Ack", "source": "AppHpa", "kind": "HorizontalPodAutoscaler"}],
        }
    )

    assert any(view["id"] == "detail_app" for view in slim["semantic_plan_scaffold"]["views"])


def test_build_llm_architecture_context_preserves_multi_vswitch_placement_scaffold():
    module = _load_script_module()

    slim = module.build_llm_architecture_context(
        {
            "target_language": {"code": "zh", "name": "Chinese"},
            "visible_nodes": [
                {"id": "Slb", "type": "ALIYUN::SLB::LoadBalancer", "label": "SLB"},
                {"id": "AppA", "type": "ALIYUN::ECS::InstanceGroup", "label": "ECS group 1"},
                {"id": "AppB", "type": "ALIYUN::ECS::InstanceGroup", "label": "ECS group 2"},
                {"id": "Rds", "type": "ALIYUN::RDS::DBInstance", "label": "RDS"},
            ],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC", "label": "VPC"},
                {"id": "VSwitchA", "type": "ALIYUN::ECS::VSwitch", "label": "VSwitch A", "parent": "Vpc"},
                {"id": "VSwitchB", "type": "ALIYUN::ECS::VSwitch", "label": "VSwitch B", "parent": "Vpc"},
            ],
            "containment": [
                {"resource": "AppA", "container": "VSwitchA"},
                {"resource": "Rds", "container": "VSwitchA"},
                {"resource": "AppB", "container": "VSwitchB"},
            ],
            "visible_edges": [
                {"from": "Slb", "to": "AppA"},
                {"from": "Slb", "to": "AppB"},
                {"from": "AppA", "to": "Rds"},
                {"from": "AppB", "to": "Rds"},
            ],
        }
    )

    assert slim["placement_summary"]["requires_contained_overview"] is True
    assert slim["semantic_plan_scaffold"]["views"][0]["layout"] == "contained"


def test_validate_semantic_plan_result_rejects_flat_overview_for_multi_vswitch_placement():
    module = _load_script_module()

    architecture_context = {
        "target_language": {"code": "zh"},
        "visible_nodes": [
            {"id": "Slb", "type": "ALIYUN::SLB::LoadBalancer"},
            {"id": "AppA", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "AppB", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Rds", "type": "ALIYUN::RDS::DBInstance"},
        ],
        "containers": [
            {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "VSwitchA", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            {"id": "VSwitchB", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
        ],
        "containment": [
            {"resource": "AppA", "container": "VSwitchA"},
            {"resource": "Rds", "container": "VSwitchA"},
            {"resource": "AppB", "container": "VSwitchB"},
        ],
        "visible_edges": [],
    }
    semantic_plan = {
        "node_labels": [],
        "edges": [
            {"from": "Slb", "to": "AppA", "kind": "traffic", "label": "后端转发"},
            {"from": "Slb", "to": "AppB", "kind": "traffic", "label": "后端转发"},
            {"from": "AppA", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
            {"from": "AppB", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
        ],
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["Slb", "AppA", "AppB", "Rds"],
                "edges": [
                    {"from": "Slb", "to": "AppA", "kind": "traffic", "label": "后端转发"},
                    {"from": "Slb", "to": "AppB", "kind": "traffic", "label": "后端转发"},
                    {"from": "AppA", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
                    {"from": "AppB", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
                ],
            }
        ],
    }

    issues = module.validate_semantic_plan_result(
        architecture_context,
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": semantic_plan["edges"],
                "rejected_edges": [],
            }
        },
    )

    assert any("placement-sensitive overview should use layout=contained" in issue for issue in issues)


def test_validate_semantic_plan_result_ignores_edges_covered_by_deterministic_edges():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [],
        "edges": [
            {"from": "Database", "to": "DBCluster", "kind": "management", "label": "数据迁移"},
            {"from": "EcsInstance", "to": "DBCluster", "kind": "traffic", "label": "访问验证"},
        ],
        "views": [
            {
                "id": "overview",
                "title": "迁移架构",
                "purpose": "展示迁移和验证",
                "layout": "flat",
                "nodes": ["Database", "DBCluster", "EcsInstance"],
                "edges": [
                    {"from": "Database", "to": "DBCluster", "kind": "management", "label": "数据迁移"},
                    {"from": "EcsInstance", "to": "DBCluster", "kind": "traffic", "label": "访问验证"},
                ],
            }
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Database", "type": "ALIYUN::RDS::DBInstance"},
                {"id": "DBCluster", "type": "ALIYUN::POLARDB::DBCluster"},
                {"id": "EcsInstance", "type": "ALIYUN::ECS::Instance"},
            ],
            "containers": [],
            "visible_edges": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "EcsInstance", "to": "DBCluster", "kind": "traffic", "label": "访问验证"}],
                "rejected_edges": [
                    {
                        "from": "Database",
                        "to": "DBCluster",
                        "reason": "covered by deterministic edge",
                    }
                ],
            }
        },
    )

    assert not any("Database->DBCluster was rejected" in issue for issue in issues)


def test_validate_semantic_plan_result_ignores_edges_covered_by_scaled_concept():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "SeedEcs", "type": "ALIYUN::ECS::Instance"},
                {"id": "ScalingGroup", "type": "ALIYUN::ESS::ScalingGroup"},
                {"id": "ScaledEcs", "type": "CONCEPT::ESS::ScaledECS"},
            ],
            "concept_nodes": [
                {
                    "id": "ScaledEcs",
                    "type": "CONCEPT::ESS::ScaledECS",
                    "source": "SeedEcs",
                    "controller": "ScalingGroup",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [{"from": "SeedEcs", "to": "ScalingGroup", "kind": "management", "label": "伸缩配置"}],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [
                    {
                        "from": "SeedEcs",
                        "to": "ScalingGroup",
                        "reason": "covered by scaled concept",
                    }
                ],
            }
        },
    )

    assert not any("SeedEcs->ScalingGroup was rejected" in issue for issue in issues)


def test_repair_semantic_plan_locally_removes_fixable_noise():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Entry", "type": "ALIYUN::ALB::LoadBalancer"},
            {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Db", "type": "ALIYUN::POLARDB::DBCluster"},
        ],
        "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
    }
    plan = {
        "node_labels": [
            {"id": "Entry", "label": "入口", "confidence": "high", "reason": "obvious"},
            {"id": "Missing", "label": "不存在", "confidence": "high", "reason": "bad"},
        ],
        "edges": [
            {"from": "Entry", "to": "App", "kind": "traffic", "label": "转发", "confidence": "high"},
            {"from": "App", "to": "Missing", "kind": "traffic", "label": "错误", "confidence": "high"},
        ],
        "views": [
            {
                "id": "detail_app",
                "layout": "deep",
                "nodes": ["Entry", "Missing"],
                "edges": [{"from": "Entry", "to": "Missing", "kind": "traffic", "label": "错误"}],
            },
            {
                "id": "overview",
                "layout": "contained",
                "nodes": ["Entry", "App"],
                "edges": [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}],
            },
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][0]["id"] == "overview"
    assert repaired["views"][1]["layout"] == "flat"
    assert repaired["node_labels"] == [{"id": "Entry", "label": "入口", "confidence": "high"}]
    assert repaired["edges"] == [
        {"from": "Entry", "to": "App", "kind": "traffic", "label": "转发", "confidence": "high"}
    ]
    assert repaired["views"][1]["nodes"] == ["Entry"]
    assert repaired["views"][1]["edges"] == []


def test_repair_semantic_plan_locally_drops_isolated_non_anchor_detail_nodes_when_edges_exist():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
            {"id": "Topic", "type": "ALIYUN::ROCKETMQ5::Topic"},
        ],
        "containers": [],
    }
    plan = {
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["App", "Redis"],
                "edges": [{"from": "App", "to": "Redis", "kind": "dependency", "label": "缓存访问"}],
            },
            {
                "id": "detail_app",
                "layout": "flat",
                "anchors": ["App"],
                "nodes": ["App", "Redis", "Topic"],
                "edges": [{"from": "App", "to": "Redis", "kind": "dependency", "label": "缓存访问"}],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][1]["nodes"] == ["App", "Redis"]


def test_repair_semantic_plan_locally_completes_detail_edges_from_overview():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "IngressEntry", "type": "CONCEPT::Kubernetes::IngressEntry"},
            {"id": "Workload", "type": "CONCEPT::Kubernetes::ApplicationWorkload"},
            {"id": "PostgreSQL", "type": "ALIYUN::RDS::DBInstance"},
            {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
            {"id": "HelmRelease", "type": "ALIYUN::CS::ClusterHelmApplication"},
        ],
        "containers": [],
    }
    plan = {
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "groups": [
                    {
                        "id": "DataBackendGroup",
                        "label": "数据后端",
                        "members": ["PostgreSQL", "Redis"],
                    }
                ],
                "nodes": ["IngressEntry", "Workload", "DataBackendGroup"],
                "edges": [
                    {"from": "IngressEntry", "to": "Workload", "kind": "traffic", "label": "入口流量"},
                    {"from": "Workload", "to": "DataBackendGroup", "kind": "traffic", "label": "数据访问"},
                ],
            },
            {
                "id": "detail_app",
                "layout": "flat",
                "anchors": ["IngressEntry", "Workload"],
                "nodes": ["IngressEntry", "Workload", "HelmRelease"],
                "edges": [{"from": "HelmRelease", "to": "Workload", "kind": "management", "label": "工作负载定义"}],
            },
            {
                "id": "detail_data",
                "layout": "flat",
                "anchors": ["Workload", "DataBackendGroup"],
                "nodes": ["Workload", "PostgreSQL", "HelmRelease"],
                "edges": [{"from": "HelmRelease", "to": "PostgreSQL", "kind": "dependency", "label": "配置绑定"}],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    detail_app = repaired["views"][1]
    assert {"from": "IngressEntry", "to": "Workload", "kind": "traffic", "label": "入口流量"} in detail_app["edges"]
    detail_data = repaired["views"][2]
    assert {"from": "Workload", "to": "PostgreSQL", "kind": "traffic", "label": "数据访问"} in detail_data["edges"]


def test_repair_semantic_plan_locally_completes_overview_edge_for_isolated_selected_node():
    module = _load_script_module()

    node_ids = ["HookIn", "HookOut", "TemplateIn", "TemplateOut", "WorkerGroup", "Role", "MasterGroup"]
    architecture_context = {
        "visible_nodes": [{"id": node_id, "type": "ALIYUN::OOS::Template"} for node_id in node_ids],
        "containers": [],
    }
    plan = {
        "edges": [
            {"from": "HookIn", "to": "WorkerGroup", "kind": "management", "label": "监听扩容"},
            {"from": "HookOut", "to": "WorkerGroup", "kind": "management", "label": "监听缩容"},
            {"from": "HookIn", "to": "TemplateIn", "kind": "management", "label": "触发扩容"},
            {"from": "HookOut", "to": "TemplateOut", "kind": "management", "label": "触发缩容"},
            {"from": "TemplateIn", "to": "Role", "kind": "dependency", "label": "执行授权"},
            {"from": "TemplateOut", "to": "Role", "kind": "dependency", "label": "执行授权"},
            {"from": "TemplateOut", "to": "MasterGroup", "kind": "management", "label": "更新配置"},
        ],
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": node_ids,
                "edges": [
                    {"from": "HookIn", "to": "WorkerGroup", "kind": "management", "label": "监听扩容"},
                    {"from": "HookOut", "to": "WorkerGroup", "kind": "management", "label": "监听缩容"},
                    {"from": "HookIn", "to": "TemplateIn", "kind": "management", "label": "触发扩容"},
                    {"from": "HookOut", "to": "TemplateOut", "kind": "management", "label": "触发缩容"},
                    {"from": "TemplateIn", "to": "Role", "kind": "dependency", "label": "执行授权"},
                    {"from": "TemplateOut", "to": "Role", "kind": "dependency", "label": "执行授权"},
                ],
            }
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert {
        "from": "TemplateOut",
        "to": "MasterGroup",
        "kind": "management",
        "label": "更新配置",
    } in repaired["views"][0]["edges"]


def test_repair_semantic_plan_locally_drops_empty_overview_containers():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Role", "type": "ALIYUN::RAM::Role"},
            {"id": "DevEcs", "type": "ALIYUN::ECS::Instance"},
            {"id": "ProdEcs", "type": "ALIYUN::ECS::Instance"},
        ],
        "containers": [
            {"id": "DevVpc", "type": "ALIYUN::ECS::VPC", "parent": None},
            {"id": "DevVSwitch", "type": "ALIYUN::ECS::VSwitch", "parent": "DevVpc"},
            {"id": "ProdVpc", "type": "ALIYUN::ECS::VPC", "parent": None},
            {"id": "ProdVSwitch", "type": "ALIYUN::ECS::VSwitch", "parent": "ProdVpc"},
        ],
        "containment": [
            {"resource": "DevEcs", "container": "DevVSwitch"},
            {"resource": "ProdEcs", "container": "ProdVSwitch"},
        ],
    }
    plan = {
        "views": [
            {
                "id": "overview",
                "layout": "contained",
                "groups": [{"id": "ComputeGroup", "label": "多环境计算", "members": ["DevEcs", "ProdEcs"]}],
                "nodes": ["DevVSwitch", "ProdVSwitch", "Role", "ComputeGroup"],
                "edges": [{"from": "Role", "to": "ComputeGroup", "kind": "management", "label": "授权"}],
            }
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][0]["nodes"] == ["Role", "ComputeGroup"]


def test_repair_semantic_plan_locally_preserves_placement_overview_layout():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
        ],
        "containers": [
            {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "VSwitch1", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            {"id": "VSwitch2", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
        ],
        "containment": [
            {"resource": "Alb", "container": "VSwitch1"},
            {"resource": "App1", "container": "VSwitch1"},
            {"resource": "App2", "container": "VSwitch2"},
        ],
    }
    plan = {
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["Alb", "App1", "App2"],
                "edges": [
                    {"from": "Alb", "to": "App1", "kind": "traffic", "label": "流量分发"},
                    {"from": "Alb", "to": "App2", "kind": "traffic", "label": "流量分发"},
                ],
            }
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][0]["layout"] == "contained"


def test_repair_semantic_plan_locally_normalizes_legacy_perspective_view_ids():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Ops", "type": "ALIYUN::ECS::RunCommand"},
        ],
        "containers": [],
        "containment": [],
    }
    plan = {
        "views": [
            {"id": "overview", "nodes": ["Alb", "App"], "edges": []},
            {
                "id": "traffic",
                "anchors": ["Alb", "App"],
                "nodes": ["Alb", "App"],
                "edges": [{"from": "Alb", "to": "App", "kind": "traffic", "label": "流量"}],
            },
            {
                "id": "operations",
                "anchors": ["App"],
                "nodes": ["App", "Ops"],
                "edges": [{"from": "Ops", "to": "App", "kind": "management", "label": "执行命令"}],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert [view["id"] for view in repaired["views"]] == ["overview", "detail_app", "detail_operations"]


def test_repair_semantic_plan_locally_infers_detail_anchors_from_overview_nodes():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Queue", "type": "ALIYUN::MNS::Queue"},
            {"id": "Command", "type": "ALIYUN::ECS::RunCommand"},
        ],
        "containers": [],
        "containment": [],
    }
    plan = {
        "views": [
            {"id": "overview", "nodes": ["App", "Queue"], "edges": []},
            {
                "id": "operations",
                "nodes": ["App", "Command"],
                "edges": [{"from": "Command", "to": "App", "kind": "management", "label": "执行命令"}],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    detail_operations = repaired["views"][1]
    assert detail_operations["id"] == "detail_operations"
    assert detail_operations["anchors"] == ["App"]


def test_repair_semantic_plan_locally_drops_detail_anchors_not_visible_in_overview():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "VpcA", "type": "ALIYUN::ECS::VPC"},
            {"id": "VpcB", "type": "ALIYUN::ECS::VPC"},
            {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            {"id": "RouteConfigA", "type": "CONCEPT::Layer::AttachmentSummary"},
            {"id": "RouteConfigB", "type": "CONCEPT::Layer::AttachmentSummary"},
        ],
        "containers": [],
        "containment": [],
    }
    plan = {
        "views": [
            {"id": "overview", "nodes": ["VpcA", "TransitRouter"], "edges": []},
            {
                "id": "detail_network",
                "anchors": ["VpcA", "VpcB"],
                "nodes": ["RouteConfigA", "RouteConfigB", "TransitRouter"],
                "edges": [
                    {"from": "RouteConfigA", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                    {"from": "RouteConfigB", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                ],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][1]["anchors"] == ["VpcA"]


def test_repair_semantic_plan_locally_keeps_route_config_edges_out_of_overview_when_detail_exists():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            {"id": "Forwarder", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "RouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
        ],
        "containers": [],
        "containment": [],
    }
    plan = {
        "edges": [
            {"from": "Vpc", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
            {"from": "Forwarder", "to": "TransitRouter", "kind": "traffic", "label": "回程流量"},
            {"from": "RouteDomain", "to": "Forwarder", "kind": "dependency", "label": "默认路由"},
        ],
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["Vpc", "TransitRouter"],
                "edges": [{"from": "Vpc", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"}],
            },
            {
                "id": "detail_network",
                "anchors": ["Vpc", "TransitRouter"],
                "nodes": ["Vpc", "TransitRouter", "RouteDomain", "Forwarder"],
                "edges": [{"from": "RouteDomain", "to": "Forwarder", "kind": "dependency", "label": "默认路由"}],
            },
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    overview = repaired["views"][0]
    assert overview["nodes"] == ["Vpc", "TransitRouter", "Forwarder"]
    assert all("RouteDomain" not in (edge["from"], edge["to"]) for edge in overview["edges"])


def test_repair_semantic_plan_locally_does_not_expand_selected_overview_group_members():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
        ],
        "containers": [],
        "containment": [],
    }
    plan = {
        "edges": [
            {"from": "Alb", "to": "App1", "kind": "traffic", "label": "转发请求"},
            {"from": "App1", "to": "Redis", "kind": "dependency", "label": "读写缓存"},
        ],
        "views": [
            {
                "id": "overview",
                "nodes": ["Alb", "AppServers", "Redis"],
                "groups": [{"id": "AppServers", "label": "应用服务器集群", "members": ["App1", "App2"]}],
                "edges": [
                    {"from": "Alb", "to": "AppServers", "kind": "traffic", "label": "分发流量"},
                    {"from": "AppServers", "to": "Redis", "kind": "dependency", "label": "缓存访问"},
                ],
            }
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    overview = repaired["views"][0]
    assert overview["nodes"] == ["Alb", "AppServers", "Redis"]
    assert all("App1" not in (edge["from"], edge["to"]) for edge in overview["edges"])


def test_repair_semantic_plan_locally_marks_cross_network_detail_edge_labels():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Forwarder", "type": "ALIYUN::ECS::Instance"},
            {"id": "FrontendRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
            {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
        ],
        "containers": [
            {"id": "SecurityVpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "FrontendVpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "Cen", "type": "ALIYUN::CEN::CenInstance"},
        ],
        "containment": [
            {"resource": "Forwarder", "container": "SecurityVpc"},
            {"resource": "FrontendRouteDomain", "container": "FrontendVpc"},
            {"resource": "TransitRouter", "container": "Cen"},
        ],
        "network_attachments": [{"type": "ALIYUN::CEN::TransitRouterVpcAttachment"}],
    }
    plan = {
        "views": [
            {
                "id": "overview",
                "nodes": ["Forwarder", "TransitRouter"],
                "edges": [],
            },
            {
                "id": "detail_network",
                "anchors": ["Forwarder", "TransitRouter"],
                "nodes": ["Forwarder", "FrontendRouteDomain", "TransitRouter"],
                "edges": [
                    {
                        "from": "Forwarder",
                        "to": "FrontendRouteDomain",
                        "kind": "dependency",
                        "label": "回程路由",
                    }
                ],
            },
        ]
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    detail_network = repaired["views"][1]
    assert detail_network["edges"][0]["label"] == "经 CEN 回程路由"


def test_repair_semantic_plan_locally_completes_compact_overview_from_top_level_edges():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Bandwidth1", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            {"id": "Bandwidth2", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
        ],
        "containers": [{"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"}],
        "containment": [],
    }
    plan = {
        "edges": [
            {"from": "Bandwidth1", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
            {"from": "Bandwidth2", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
        ],
        "views": [
            {
                "id": "overview",
                "layout": "contained",
                "nodes": ["layer_CenInstance_Config"],
                "edges": [],
            }
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    assert repaired["views"][0]["nodes"] == ["layer_CenInstance_Config", "Bandwidth1", "Bandwidth2"]
    assert repaired["views"][0]["edges"] == [
        {"from": "Bandwidth1", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
        {"from": "Bandwidth2", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
    ]


def test_repair_semantic_plan_locally_replaces_noisy_container_overview_with_business_edges():
    module = _load_script_module()

    architecture_context = {
        "visible_nodes": [
            {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
            {"id": "Rds", "type": "ALIYUN::RDS::DBInstance"},
            {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
        ],
        "containers": [
            {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
            {"id": "VSwitch1", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            {"id": "VSwitch2", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            {"id": "VSwitch3", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            {"id": "VSwitch4", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
        ],
        "containment": [
            {"resource": "Alb", "container": "VSwitch3"},
            {"resource": "App1", "container": "VSwitch1"},
            {"resource": "App2", "container": "VSwitch2"},
            {"resource": "Rds", "container": "VSwitch4"},
            {"resource": "Nat", "container": "VSwitch4"},
        ],
    }
    plan = {
        "edges": [
            {"from": "Alb", "to": "App1", "kind": "traffic", "label": "HTTP 转发"},
            {"from": "Alb", "to": "App2", "kind": "traffic", "label": "HTTP 转发"},
            {"from": "App1", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
            {"from": "App2", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
            {"from": "App1", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
            {"from": "App2", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
        ],
        "views": [
            {
                "id": "overview",
                "layout": "contained",
                "nodes": ["VSwitch1", "VSwitch2", "VSwitch3", "VSwitch4"],
                "edges": [
                    {"from": "VSwitch3", "to": "VSwitch1", "kind": "traffic", "label": "ALB 转发"},
                    {"from": "VSwitch1", "to": "VSwitch4", "kind": "dependency", "label": "DB 访问"},
                ],
            },
            {
                "id": "detail_app",
                "layout": "flat",
                "anchors": ["VSwitch3", "VSwitch1", "VSwitch2"],
                "nodes": ["Alb", "App1", "App2"],
                "edges": [
                    {"from": "Alb", "to": "App1", "kind": "traffic", "label": "HTTP 转发"},
                    {"from": "Alb", "to": "App2", "kind": "traffic", "label": "HTTP 转发"},
                ],
            },
        ],
    }

    repaired = module.repair_semantic_plan_locally(architecture_context, plan)

    overview = repaired["views"][0]
    assert overview["nodes"] == ["Alb", "App1", "App2", "Rds", "Nat"]
    assert all(not node_id.startswith("VSwitch") for node_id in overview["nodes"])
    assert overview["edges"] == [
        {"from": "Alb", "to": "App1", "kind": "traffic", "label": "HTTP 转发"},
        {"from": "Alb", "to": "App2", "kind": "traffic", "label": "HTTP 转发"},
        {"from": "App1", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
        {"from": "App2", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
        {"from": "App1", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
        {"from": "App2", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
    ]
    assert [view["id"] for view in repaired["views"]] == ["overview"]


def test_repair_semantic_plan_locally_drops_top_level_container_edges_but_keeps_view_edges():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
        },
        {
            "node_labels": [],
            "edges": [{"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}],
            "views": [
                {
                    "id": "overview",
                    "nodes": ["layer_CenInstance_Config"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "anchors": ["layer_CenInstance_Config"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}
                    ],
                },
            ],
        },
    )

    assert repaired.get("edges") is None
    assert repaired["views"][1]["edges"] == [
        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}
    ]


def test_repair_semantic_plan_locally_marks_external_cen_network_edge():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "ExternalVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_instance_id": "Ref:OtherVpcId",
                },
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联配置", "confidence": "high"}],
            "views": [
                {
                    "id": "overview",
                    "nodes": ["Vpc"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "anchors": ["Vpc"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}
                    ],
                },
            ],
        },
    )

    assert repaired["views"][1]["edges"] == [
        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "连接外部VPC/VBR"}
    ]


def test_repair_semantic_plan_locally_marks_external_cen_network_edge_in_small_overview():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "ExternalVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_instance_id": "Ref:OtherVpcId",
                },
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联配置", "confidence": "high"}],
            "views": [
                {
                    "id": "overview",
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}
                    ],
                }
            ],
        },
    )

    assert repaired["views"][0]["edges"] == [
        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "连接外部VPC/VBR"}
    ]


def test_repair_semantic_plan_locally_connects_split_route_forwarder_chain():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "Forwarder", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "IngressRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "EgressRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [],
            "network_attachments": [
                {
                    "id": "VpcAttachment",
                    "type": "ALIYUN::CEN::TransitRouterVpcAttachment",
                    "network": "CEN",
                    "transit_router": "TransitRouter",
                    "child_instance_type": "VPC",
                    "child_resource": "VpcSec",
                }
            ],
            "explicit_relations": [
                {
                    "source": "RouteToForwarder",
                    "source_type": "ALIYUN::ECS::Route",
                    "property": "NextHopId",
                    "target": "Forwarder",
                    "target_type": "ALIYUN::ECS::InstanceGroup",
                },
                {
                    "source": "RouteToCen",
                    "source_type": "ALIYUN::ECS::Route",
                    "property": "NextHopId",
                    "target": "VpcAttachment",
                    "target_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
                },
            ],
            "route_intents": [
                {
                    "id": "RouteToForwarder",
                    "type": "ALIYUN::ECS::Route",
                    "destination": "0.0.0.0/0",
                    "next_hop_type": "Instance",
                    "next_hop_resource": "Forwarder",
                    "next_hop_resource_type": "ALIYUN::ECS::InstanceGroup",
                },
                {
                    "id": "RouteToCen",
                    "type": "ALIYUN::ECS::Route",
                    "destination": "0.0.0.0/0",
                    "next_hop_type": "Attachment",
                    "next_hop_resource": "VpcAttachment",
                    "next_hop_resource_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
                },
            ],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "flat",
                    "nodes": ["Forwarder", "TransitRouter"],
                    "edges": [{"from": "Forwarder", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"}],
                },
                {
                    "id": "detail_network",
                    "title": "安全 VPC 路由与转发详情",
                    "layout": "flat",
                    "anchors": ["Forwarder", "TransitRouter"],
                    "nodes": ["Forwarder", "IngressRouteDomain", "EgressRouteDomain", "TransitRouter"],
                    "edges": [
                        {
                            "from": "Forwarder",
                            "to": "IngressRouteDomain",
                            "kind": "dependency",
                            "label": "出网路由下一跳",
                        },
                        {
                            "from": "EgressRouteDomain",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "回程路由指向 CEN",
                        },
                    ],
                },
            ],
        },
    )

    detail_network = repaired["views"][1]
    assert {
        "from": "Forwarder",
        "to": "EgressRouteDomain",
        "kind": "dependency",
        "label": "回程路由",
    } in detail_network["edges"]
    assert module._semantic_view_has_path(
        "Forwarder",
        {"TransitRouter"},
        detail_network["edges"],
    )


def test_repair_semantic_plan_locally_prefers_return_route_over_vpc_container_bridge():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "Forwarder", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "VpcRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "SubnetReturnRoute", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [{"id": "VpcSec", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "VpcAttachment",
                    "type": "ALIYUN::CEN::TransitRouterVpcAttachment",
                    "network": "CEN",
                    "transit_router": "TransitRouter",
                    "child_instance_type": "VPC",
                    "child_resource": "VpcSec",
                }
            ],
            "explicit_relations": [
                {
                    "source": "RouteToForwarder",
                    "source_type": "ALIYUN::ECS::Route",
                    "property": "NextHopId",
                    "target": "Forwarder",
                    "target_type": "ALIYUN::ECS::InstanceGroup",
                }
            ],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "nodes": ["VpcSec", "Forwarder", "TransitRouter"],
                    "edges": [{"from": "VpcSec", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"}],
                },
                {
                    "id": "detail_network",
                    "title": "安全 VPC 路由与转发详情",
                    "layout": "flat",
                    "anchors": ["VpcSec", "Forwarder", "TransitRouter"],
                    "nodes": ["VpcSec", "Forwarder", "VpcRouteDomain", "SubnetReturnRoute", "TransitRouter"],
                    "edges": [
                        {"from": "VpcSec", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "VpcRouteDomain", "to": "Forwarder", "kind": "dependency", "label": "下一跳指向"},
                        {"from": "VpcSec", "to": "Forwarder", "kind": "traffic", "label": "主路由表转发"},
                        {"from": "VpcRouteDomain", "to": "TransitRouter", "kind": "dependency", "label": "出向 CEN"},
                        {
                            "from": "SubnetReturnRoute",
                            "to": "VpcRouteDomain",
                            "kind": "dependency",
                            "label": "关联路由表",
                        },
                        {"from": "Forwarder", "to": "VpcSec", "kind": "traffic", "label": "回程路由"},
                    ],
                },
            ],
        },
    )

    detail_network = repaired["views"][1]
    assert {
        "from": "Forwarder",
        "to": "SubnetReturnRoute",
        "kind": "dependency",
        "label": "回程路由",
    } in detail_network["edges"]


def test_repair_semantic_plan_locally_reverses_detail_network_transit_router_source_edge():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "RouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "VpcSec", "type": "ALIYUN::ECS::VPC"}],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "nodes": ["VpcSec", "TransitRouter"],
                    "edges": [{"from": "VpcSec", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"}],
                },
                {
                    "id": "detail_network",
                    "layout": "flat",
                    "anchors": ["VpcSec", "TransitRouter"],
                    "nodes": ["VpcSec", "TransitRouter", "RouteDomain"],
                    "edges": [
                        {"from": "TransitRouter", "to": "VpcSec", "kind": "traffic", "label": "回程路由"},
                        {"from": "RouteDomain", "to": "TransitRouter", "kind": "dependency", "label": "出网路由"},
                    ],
                },
            ],
        },
    )

    detail_network = repaired["views"][1]
    assert {
        "from": "VpcSec",
        "to": "TransitRouter",
        "kind": "dependency",
        "label": "CEN 接入",
    } in detail_network["edges"]
    assert {
        "from": "TransitRouter",
        "to": "VpcSec",
        "kind": "traffic",
        "label": "回程路由",
    } not in detail_network["edges"]


def test_repair_semantic_plan_locally_drops_redundant_small_group_detail_view():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
        },
        {
            "node_labels": [],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup", "Vpc"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
                },
                {
                    "id": "detail_network",
                    "title": "网络互联详情",
                    "layout": "flat",
                    "anchors": ["Vpc"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "连接外部网络"}
                    ],
                },
                {
                    "id": "detail_app",
                    "title": "应用负载详情",
                    "layout": "flat",
                    "anchors": ["LoadBalancer", "BackendGroup"],
                    "nodes": ["LoadBalancer", "BackendServer1", "BackendServer2"],
                    "edges": [
                        {"from": "LoadBalancer", "to": "BackendServer1", "kind": "traffic", "label": "后端转发"},
                        {"from": "LoadBalancer", "to": "BackendServer2", "kind": "traffic", "label": "后端转发"},
                    ],
                },
            ],
        },
    )

    assert [view["id"] for view in repaired["views"]] == ["overview", "detail_network"]


def test_repair_semantic_plan_locally_drops_detail_that_repeats_overview_endpoints():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "DeployTemplate", "type": "ALIYUN::OOS::Template"},
                {"id": "MasterGroup", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "LifecycleHook", "type": "ALIYUN::ESS::LifecycleHook"},
                {"id": "WorkerGroup", "type": "ALIYUN::ESS::ScalingGroup"},
            ],
            "containers": [],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "title": "Kafka 集群架构概览",
                    "layout": "flat",
                    "nodes": ["DeployTemplate", "MasterGroup", "LifecycleHook", "WorkerGroup"],
                    "edges": [
                        {"from": "DeployTemplate", "to": "MasterGroup", "kind": "management", "label": "部署管理节点"},
                        {"from": "LifecycleHook", "to": "WorkerGroup", "kind": "management", "label": "触发伸缩事件"},
                    ],
                },
                {
                    "id": "detail_operations",
                    "title": "集群运维与伸缩详情",
                    "layout": "flat",
                    "anchors": ["DeployTemplate", "MasterGroup", "LifecycleHook", "WorkerGroup"],
                    "nodes": ["DeployTemplate", "MasterGroup", "LifecycleHook", "WorkerGroup"],
                    "edges": [
                        {"from": "DeployTemplate", "to": "MasterGroup", "kind": "management", "label": "初始化环境"},
                        {"from": "LifecycleHook", "to": "WorkerGroup", "kind": "management", "label": "监听实例状态"},
                    ],
                },
            ],
        },
    )

    assert [view["id"] for view in repaired["views"]] == ["overview"]


def test_repair_semantic_plan_locally_keeps_non_redundant_public_network_app_detail():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "DmzNlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "ProdAlb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "ProdAlb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App1", "type": "ALIYUN::ECS::Instance"},
                {"id": "App2", "type": "ALIYUN::ECS::Instance"},
                {"id": "App3", "type": "ALIYUN::ECS::Instance"},
                {"id": "App4", "type": "ALIYUN::ECS::Instance"},
            ],
            "containers": [],
        },
        {
            "node_labels": [],
            "edges": [
                {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 后端转发"},
                {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "跨 VPC 负载均衡架构概览",
                    "layout": "flat",
                    "groups": [
                        {"id": "ProdAlbGroup", "label": "生产 ALB 集群", "members": ["ProdAlb1", "ProdAlb2"]},
                        {
                            "id": "ProdAppGroup",
                            "label": "生产应用集群",
                            "members": ["App1", "App2", "App3", "App4"],
                        },
                    ],
                    "nodes": ["DmzNlb", "ProdAlbGroup", "ProdAppGroup"],
                    "edges": [
                        {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 后端转发"},
                        {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                    ],
                },
                {
                    "id": "detail_app",
                    "title": "生产应用负载分发详情",
                    "layout": "flat",
                    "anchors": ["DmzNlb", "ProdAlbGroup", "ProdAppGroup"],
                    "groups": [
                        {
                            "id": "ProdAppGroup",
                            "label": "生产应用集群",
                            "members": ["App1", "App2", "App3", "App4"],
                        }
                    ],
                    "nodes": ["DmzNlb", "ProdAlb1", "ProdAlb2", "ProdAppGroup"],
                    "edges": [
                        {"from": "DmzNlb", "to": "ProdAlb1", "kind": "traffic", "label": "经 CEN 后端转发"},
                        {"from": "DmzNlb", "to": "ProdAlb2", "kind": "traffic", "label": "经 CEN 后端转发"},
                        {"from": "ProdAlb1", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                        {"from": "ProdAlb2", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                    ],
                },
            ],
        },
    )

    assert [view["id"] for view in repaired["views"]] == ["overview", "detail_app"]


def test_repair_semantic_plan_locally_drops_nat_to_load_balancer_overview_edge():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App", "type": "ALIYUN::ECS::Instance"},
                {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteConfig", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
        },
        {
            "node_labels": [],
            "edges": [
                {"from": "Nlb", "to": "Alb", "kind": "traffic", "label": "经 CEN 转发"},
                {"from": "Alb", "to": "App", "kind": "traffic", "label": "后端转发"},
                {"from": "Nat", "to": "Nlb", "kind": "dependency", "label": "DMZ 出口"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "layout": "flat",
                    "nodes": ["Nlb", "Alb", "App", "Nat"],
                    "edges": [
                        {"from": "Nlb", "to": "Alb", "kind": "traffic", "label": "经 CEN 转发"},
                        {"from": "Alb", "to": "App", "kind": "traffic", "label": "后端转发"},
                        {"from": "Nat", "to": "Nlb", "kind": "dependency", "label": "DMZ 出口"},
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "layout": "flat",
                    "anchors": ["Nlb", "Alb"],
                    "nodes": ["Nat", "DmzRouteConfig", "TransitRouter"],
                    "edges": [
                        {"from": "Nat", "to": "DmzRouteConfig", "kind": "traffic", "label": "SNAT 路由"},
                        {"from": "DmzRouteConfig", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                    ],
                },
            ],
        },
    )

    overview = repaired["views"][0]
    assert "Nat" not in overview["nodes"]
    assert all({edge.get("from"), edge.get("to")} != {"Nat", "Nlb"} for edge in overview["edges"])


def test_repair_semantic_plan_locally_merges_small_network_detail_into_overview():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup", "Vpc"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "layout": "flat",
                    "anchors": ["Vpc"],
                    "nodes": ["Vpc", "layer_CenInstance_Config", "CenBandwidthPackage"],
                    "edges": [
                        {
                            "from": "Vpc",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "连接外部VPC/VBR",
                        },
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "management",
                            "label": "带宽绑定",
                        },
                    ],
                },
            ],
        },
    )

    assert [view["id"] for view in repaired["views"]] == ["overview"]
    overview = repaired["views"][0]
    assert overview["nodes"] == [
        "LoadBalancer",
        "BackendGroup",
        "Vpc",
        "layer_CenInstance_Config",
        "CenBandwidthPackage",
    ]
    assert overview["edges"] == [
        {"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"},
        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "连接外部VPC/VBR"},
        {"from": "CenBandwidthPackage", "to": "layer_CenInstance_Config", "kind": "management", "label": "带宽绑定"},
    ]


def test_repair_semantic_plan_locally_merges_small_network_detail_without_expanding_summary_members():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        },
                        {
                            "id": "CenDomain",
                            "label": "云企业网互联域",
                            "members": ["layer_CenInstance_Config", "CenBandwidthPackage"],
                        },
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup", "Vpc", "CenDomain"],
                    "edges": [
                        {"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"},
                        {"from": "Vpc", "to": "CenDomain", "kind": "dependency", "label": "跨网互联"},
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "layout": "flat",
                    "anchors": ["Vpc", "CenDomain"],
                    "nodes": ["Vpc", "layer_CenInstance_Config", "CenBandwidthPackage"],
                    "edges": [
                        {
                            "from": "layer_CenInstance_Config",
                            "to": "Vpc",
                            "kind": "dependency",
                            "label": "连接外部VPC/VBR",
                        },
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "management",
                            "label": "带宽绑定",
                        },
                    ],
                },
            ],
        },
    )

    assert [view["id"] for view in repaired["views"]] == ["overview"]
    overview = repaired["views"][0]
    assert overview["nodes"] == ["LoadBalancer", "BackendGroup", "Vpc", "CenDomain"]
    assert overview["edges"] == [
        {"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"},
        {"from": "Vpc", "to": "CenDomain", "kind": "dependency", "label": "连接外部VPC/VBR"},
    ]


def test_repair_semantic_plan_locally_drops_attachment_marker_repeated_with_layer_summary():
    module = _load_script_module()

    repaired = module.repair_semantic_plan_locally(
        {
            "visible_nodes": [
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            ],
            "containers": [{"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"}],
            "attachments": [
                {
                    "via": "CenBandwidthPackageAssociation",
                    "marker": "CenBandwidthPackage",
                    "target": "CenInstance",
                    "property": "CenId",
                }
            ],
        },
        {
            "node_labels": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "nodes": ["layer_CenInstance_Config", "CenBandwidthPackage"],
                    "edges": [
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "management",
                            "label": "带宽绑定",
                        }
                    ],
                }
            ],
        },
    )

    assert repaired["views"][0]["nodes"] == ["layer_CenInstance_Config"]
    assert repaired["views"][0]["edges"] == []


def test_validate_semantic_plan_result_allows_small_cen_network_in_single_overview():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup", "Vpc", "layer_CenInstance_Config", "CenBandwidthPackage"],
                    "edges": [
                        {"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"},
                        {
                            "from": "Vpc",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "连接外部VPC/VBR",
                        },
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "management",
                            "label": "带宽绑定",
                        },
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("detail_network" in issue for issue in issues)
    assert not any("detail_<area>" in issue for issue in issues)


def test_retry_policy_stops_after_second_attempt_for_minor_issues():
    module = _load_script_module()

    assert module.should_retry_semantic_plan_attempt(1, 3, ["edge label for A->B is too long: abc"]) is True
    assert module.should_retry_semantic_plan_attempt(2, 3, ["edge label for A->B is too long: abc"]) is False
    assert (
        module.should_retry_semantic_plan_attempt(
            2,
            3,
            ["complex architecture should define overview plus drill-down detail views"],
        )
        is True
    )
    assert module.should_retry_semantic_plan_attempt(3, 3, ["LLM output was not valid semantic_plan JSON"]) is False


def test_retry_policy_treats_isolated_overview_nodes_as_severe():
    module = _load_script_module()

    assert (
        module.should_retry_semantic_plan_attempt(
            2,
            3,
            ["view overview has isolated nodes layer_CenInstance_Config; connect them or move them to detail"],
        )
        is True
    )


def test_validation_issue_score_prefers_fewer_severe_issues():
    module = _load_script_module()

    assert module.validation_issue_score(
        ["view overview has isolated nodes layer_CenInstance_Config; connect them or move them to detail"]
    ) > module.validation_issue_score(["view detail_network should include anchored network domains BackendGroup"])


def test_build_semantic_plan_user_prompt_includes_retry_feedback():
    module = _load_script_module()

    prompt = module.build_semantic_plan_user_prompt(
        {"target_language": {"code": "zh"}, "visible_nodes": [{"id": "ECS"}]},
        previous_plan={"node_labels": [{"id": "ECS", "label": "APP01"}], "edges": []},
        validation_issues=["node label for ECS copies raw identifier APP01"],
    )

    assert "Revise the previous semantic_plan" in prompt
    assert "node label for ECS copies raw identifier APP01" in prompt
    assert '"label": "APP01"' in prompt


def test_build_semantic_plan_user_prompt_keeps_fact_bundle_in_cacheable_prefix():
    module = _load_script_module()

    architecture_context = {
        "target_language": {"code": "zh"},
        "visible_nodes": [{"id": "ECS"}, {"id": "RDS"}],
        "explicit_relations": [{"source": "ECS", "target": "RDS"}],
    }
    first_prompt = module.build_semantic_plan_user_prompt(architecture_context)
    retry_prompt = module.build_semantic_plan_user_prompt(
        architecture_context,
        previous_plan={"node_labels": [{"id": "ECS", "label": "APP01"}], "edges": []},
        validation_issues=["node label for ECS copies raw identifier APP01"],
    )

    assert first_prompt.startswith("Architecture fact bundle:\n")
    assert retry_prompt.startswith("Architecture fact bundle:\n")
    assert module.DYNAMIC_BOUNDARY in first_prompt
    assert module.DYNAMIC_BOUNDARY in retry_prompt
    assert retry_prompt.index("Validation issues:") > retry_prompt.index(module.DYNAMIC_BOUNDARY)

    common_prefix_chars = 0
    for left, right in zip(first_prompt, retry_prompt):
        if left != right:
            break
        common_prefix_chars += 1
    assert common_prefix_chars > first_prompt.index(module.DYNAMIC_BOUNDARY)


def test_build_semantic_plan_user_prompt_can_build_append_retry_instruction_without_facts():
    module = _load_script_module()

    prompt = module.build_semantic_plan_user_prompt(
        {"target_language": {"code": "zh"}, "visible_nodes": [{"id": "ECS"}]},
        attempt=2,
        validation_issues=["node label for ECS copies raw identifier APP01"],
        include_fact_bundle=False,
        include_previous_plan=False,
    )

    assert prompt.startswith("Attempt 2 instruction:")
    assert "Architecture fact bundle" not in prompt
    assert module.DYNAMIC_BOUNDARY not in prompt
    assert "Previous semantic_plan" not in prompt
    assert "node label for ECS copies raw identifier APP01" in prompt


def test_prompt_debug_html_shows_appended_conversation_order(tmp_path):
    module = _load_script_module()

    path = tmp_path / "prompt-debug.html"
    first_user = (
        "Architecture fact bundle:\n"
        '{"visible_nodes":[]}\n\n'
        f"{module.DYNAMIC_BOUNDARY}\n\n"
        "Attempt 1 instruction:\nCreate a semantic_plan"
    )
    module.write_prompt_debug_html(
        path,
        title="demo.yml",
        model="test-model",
        records=[
            {
                "attempt": 2,
                "selected": True,
                "llm_seconds": 2.5,
                "system_prompt": "SYSTEM <rules>",
                "user_prompt": "Attempt 2 instruction:\nRevise the previous semantic_plan",
                "messages": [
                    {"role": "user", "content": first_user},
                    {"role": "assistant", "content": '{"views":[]}'},
                    {"role": "user", "content": "Attempt 2 instruction:\nRevise the previous semantic_plan"},
                ],
                "sent_validation_issues": ["missing overview"],
                "validation_issues": [],
                "raw_output": '{"views":[{"id":"overview"}]}',
            },
        ],
        timings={"llm": 2.5, "total": 3.0},
    )

    html = path.read_text(encoding="utf-8")
    assert "Prompt Sent Order" in html
    assert "1. System Prompt" in html
    assert "2. User Message 1 Cacheable Prefix" in html
    assert "3. Dynamic Boundary" in html
    assert "4. User Message 1 Dynamic Instruction" in html
    assert "5. Assistant Message 1" in html
    assert "6. User Message 2" in html
    assert "Full Request Prompt" in html
    assert "system:" in html
    assert "SYSTEM &lt;rules&gt;" in html
    assert "assistant:" in html
    assert "{&quot;views&quot;:[]}" in html
    assert "user:" in html
    assert "Attempt 2 instruction:" in html
    assert html.index("2. User Message 1 Cacheable Prefix") < html.index("5. Assistant Message 1")
    assert html.index("5. Assistant Message 1") < html.index("6. User Message 2")


def test_write_prompt_debug_html_contains_attempt_prompts(tmp_path):
    module = _load_script_module()

    path = tmp_path / "prompt-debug.html"
    module.write_prompt_debug_html(
        path,
        title="demo.yml",
        model="test-model",
        records=[
            {
                "attempt": 1,
                "selected": False,
                "llm_seconds": 1.25,
                "system_prompt": "SYSTEM <rules>",
                "user_prompt": (
                    "Architecture fact bundle:\n"
                    '{"visible_nodes":[]}\n\n'
                    f"{module.DYNAMIC_BOUNDARY}\n\n"
                    "Attempt 1 instruction:\nCreate a semantic_plan"
                ),
                "sent_validation_issues": [],
                "validation_issues": ["missing overview"],
                "raw_output": '{"views":[]}',
            },
            {
                "attempt": 2,
                "selected": True,
                "llm_seconds": 2.5,
                "system_prompt": "SYSTEM <rules>",
                "user_prompt": (
                    "Architecture fact bundle:\n"
                    '{"visible_nodes":[]}\n\n'
                    f"{module.DYNAMIC_BOUNDARY}\n\n"
                    "Attempt 2 instruction:\nRevise the previous semantic_plan"
                ),
                "sent_validation_issues": ["missing overview"],
                "validation_issues": [],
                "raw_output": '{"views":[{"id":"overview"}]}',
            },
        ],
        timings={"llm": 3.75, "total": 4.0},
    )

    html = path.read_text(encoding="utf-8")
    assert "Prompt Debug: demo.yml" in html
    assert "test-model" in html
    assert "Attempt 1" in html
    assert "Attempt 2" in html
    assert "selected" in html
    assert "SYSTEM &lt;rules&gt;" in html
    assert "Create a semantic_plan" in html
    assert "missing overview" in html
    assert "Prompt Sent Order" in html
    assert "Full Request Prompt" in html
    assert "1. System Prompt" in html
    assert "2. User Message 1 Cacheable Prefix" in html
    assert "3. Dynamic Boundary" in html
    assert "4. User Message 1 Dynamic Instruction" in html
    assert html.index("1. System Prompt") < html.index("2. User Message 1 Cacheable Prefix")
    assert html.index("2. User Message 1 Cacheable Prefix") < html.index("3. Dynamic Boundary")
    assert html.index("3. Dynamic Boundary") < html.index("4. User Message 1 Dynamic Instruction")
    assert module.DYNAMIC_BOUNDARY in html
    assert "<summary>System Prompt</summary>" not in html
    assert "<summary>Full User Prompt</summary>" not in html
    assert "<summary>Cacheable User Prefix</summary>" not in html
    assert "<summary>Dynamic User Instruction</summary>" not in html
    assert "<summary>Previous Plan Sent</summary>" not in html


def test_system_prompt_guides_scaled_compute_semantics():
    module = _load_script_module()

    assert "views" in module.SYSTEM_PROMPT
    assert "layout" in module.SYSTEM_PROMPT
    assert 'layout="flat"' in module.SYSTEM_PROMPT
    assert 'layout="contained"' in module.SYSTEM_PROMPT
    assert "overview" in module.SYSTEM_PROMPT
    assert "drill-down" in module.SYSTEM_PROMPT
    assert "anchors" in module.SYSTEM_PROMPT
    assert "summary groups" in module.SYSTEM_PROMPT
    assert "经 CEN 后端转发" in module.SYSTEM_PROMPT
    assert "生产 VPC -> CEN -> DMZ NAT" in module.SYSTEM_PROMPT
    assert "detail_app" in module.SYSTEM_PROMPT
    assert "Do not create separate peer views named traffic" in module.SYSTEM_PROMPT
    assert "property_references" in module.SYSTEM_PROMPT
    assert "orchestration_actions" in module.SYSTEM_PROMPT
    assert "one orchestration action targets multiple compute resources" in module.SYSTEM_PROMPT
    assert "挂载 NAS（云助手）" in module.SYSTEM_PROMPT
    assert "concept_nodes" in module.SYSTEM_PROMPT
    assert "node_labels" in module.SYSTEM_PROMPT
    assert "replace only the main node title" in module.SYSTEM_PROMPT
    assert "target_language" in module.SYSTEM_PROMPT
    assert "Do not copy raw identifier-like names" in module.SYSTEM_PROMPT
    assert "not on the scaling controller" in module.SYSTEM_PROMPT
    assert "Route.NextHopId" in module.SYSTEM_PROMPT
    assert "route_intents" in module.SYSTEM_PROMPT
    assert "安全转发网关 -> 安全子网回程路由 -> CEN 转发路由器" in module.SYSTEM_PROMPT
    assert "Avoid repeated fan-in or fan-out edges" in module.SYSTEM_PROMPT
    assert "Do not use CEN/Transit Router as the target of business traffic" in module.SYSTEM_PROMPT
    assert "NAT Gateway/SNAT is outbound access" in module.SYSTEM_PROMPT
    assert "multi-VSwitch architectures" in module.SYSTEM_PROMPT
    assert "DMZ VPC 路由配置" in module.SYSTEM_PROMPT
    assert "do not make CEN/Transit Router the visual source" in module.SYSTEM_PROMPT
    assert "VPC/domain/route summary -> CEN/Transit Router" in module.SYSTEM_PROMPT
    assert "Keep route/config domain nodes out of overview" in module.SYSTEM_PROMPT
    assert "do not also include CEN/Transit Router as an overview node" in module.SYSTEM_PROMPT
    assert "In detail_network, also avoid CEN/Transit Router as a traffic source" in module.SYSTEM_PROMPT


def test_validate_semantic_plan_result_flags_raw_identifier_labels_in_chinese():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "node_label_hints": [
                {"id": "App", "hints": {"InstanceName": "APP01"}},
                {"id": "Alb", "hints": {"LoadBalancerName": "ALB_HZ_1"}},
            ],
        },
        {"node_labels": [{"id": "App", "label": "APP01"}, {"id": "Alb", "label": "ALB_HZ_1"}], "edges": []},
        {
            "semantic_plan": {
                "accepted_node_labels": [
                    {"id": "App", "label": "APP01"},
                    {"id": "Alb", "label": "ALB_HZ_1"},
                ],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("APP01" in issue for issue in issues)
    assert any("ALB_HZ_1" in issue for issue in issues)


def test_validate_semantic_plan_result_accepts_chinese_role_labels():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "node_label_hints": [{"id": "App", "hints": {"InstanceName": "APP01"}}],
        },
        {"node_labels": [{"id": "App", "label": "应用服务器 1"}], "edges": []},
        {
            "semantic_plan": {
                "accepted_node_labels": [{"id": "App", "label": "应用服务器 1"}],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "App", "to": "Db", "label": "访问数据库"}],
                "rejected_edges": [],
            }
        },
    )

    assert issues == []


def test_validate_semantic_plan_result_flags_missing_edges_for_related_architecture():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [{"id": "Alb"}, {"id": "App"}, {"id": "Db"}],
            "concept_nodes": [],
            "explicit_relations": [
                {
                    "source": "Alb",
                    "target": "App",
                    "source_type": "ALIYUN::ALB::LoadBalancer",
                    "target_type": "ALIYUN::ECS::InstanceGroup",
                },
                {
                    "source": "App",
                    "target": "Db",
                    "source_type": "ALIYUN::ECS::InstanceGroup",
                    "target_type": "ALIYUN::POLARDB::DBCluster",
                },
            ],
        },
        {"node_labels": [], "edges": []},
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("semantic_plan has no edges" in issue for issue in issues)


def test_validate_semantic_plan_result_accepts_relationships_defined_only_in_views():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [],
        "edges": [],
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["LoadBalancer", "BackendGroup"],
                "edges": [
                    {
                        "from": "LoadBalancer",
                        "to": "BackendGroup",
                        "kind": "traffic",
                        "label": "后端转发",
                    }
                ],
            }
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendGroup", "type": "CONCEPT::ApplicationGroup"},
                {"id": "CenConfig", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "visible_edges": [{"from": "LoadBalancer", "to": "BackendGroup"}],
            "explicit_relations": [
                {
                    "source": "Attachment",
                    "target": "LoadBalancer",
                    "source_type": "ALIYUN::SLB::BackendServerAttachment",
                },
                {
                    "source": "BandwidthPackageAssociation",
                    "target": "CenConfig",
                    "source_type": "ALIYUN::CEN::CenBandwidthPackageAssociation",
                },
            ],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("semantic_plan has no edges" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_partial_shared_orchestration_dependency():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
                {"id": "Database", "type": "ALIYUN::POLARDB::DBCluster"},
            ],
            "explicit_relations": [],
            "orchestration_actions": [
                {
                    "id": "RunCommand",
                    "type": "ALIYUN::ECS::RunCommand",
                    "targets": [
                        {
                            "id": "App1",
                            "type": "ALIYUN::ECS::InstanceGroup",
                            "property": "InstanceIds",
                            "visible": True,
                        },
                        {
                            "id": "App2",
                            "type": "ALIYUN::ECS::InstanceGroup",
                            "property": "InstanceIds",
                            "visible": True,
                        },
                    ],
                    "referenced_resources": [
                        {
                            "id": "Redis",
                            "type": "ALIYUN::REDIS::Instance",
                            "property": "CommandContent",
                            "visible": True,
                        },
                        {
                            "id": "Database",
                            "type": "ALIYUN::POLARDB::DBCluster",
                            "property": "CommandContent",
                            "visible": True,
                        },
                    ],
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [
                {
                    "from": "App1",
                    "to": "Redis",
                    "kind": "dependency",
                    "label": "缓存访问",
                    "confidence": "medium",
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [
                    {
                        "from": "App1",
                        "to": "Redis",
                        "kind": "dependency",
                        "label": "缓存访问",
                        "confidence": "medium",
                    }
                ],
                "rejected_edges": [],
            }
        },
    )

    assert any("RunCommand shares Redis dependency across App1, App2" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_grouped_shared_orchestration_dependency():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
            ],
            "explicit_relations": [],
            "orchestration_actions": [
                {
                    "id": "RunCommand",
                    "type": "ALIYUN::ECS::RunCommand",
                    "targets": [
                        {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup", "visible": True},
                        {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup", "visible": True},
                    ],
                    "referenced_resources": [
                        {"id": "Redis", "type": "ALIYUN::REDIS::Instance", "visible": True},
                    ],
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "应用概览",
                    "purpose": "overview",
                    "layout": "flat",
                    "nodes": ["BackendGroup", "Redis"],
                    "groups": [{"id": "BackendGroup", "label": "后端服务组", "members": ["App1", "App2"]}],
                    "edges": [
                        {
                            "from": "BackendGroup",
                            "to": "Redis",
                            "kind": "dependency",
                            "label": "缓存访问",
                        }
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("RunCommand shares Redis dependency" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_partial_shared_dependency_inside_detail_view():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
            ],
            "explicit_relations": [],
            "orchestration_actions": [
                {
                    "id": "RunCommand",
                    "type": "ALIYUN::ECS::RunCommand",
                    "targets": [
                        {"id": "App1", "type": "ALIYUN::ECS::InstanceGroup", "visible": True},
                        {"id": "App2", "type": "ALIYUN::ECS::InstanceGroup", "visible": True},
                    ],
                    "referenced_resources": [
                        {"id": "Redis", "type": "ALIYUN::REDIS::Instance", "visible": True},
                    ],
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "应用概览",
                    "purpose": "overview",
                    "layout": "flat",
                    "nodes": ["BackendGroup", "Redis"],
                    "groups": [{"id": "BackendGroup", "label": "后端服务组", "members": ["App1", "App2"]}],
                    "edges": [
                        {
                            "from": "BackendGroup",
                            "to": "Redis",
                            "kind": "dependency",
                            "label": "缓存访问",
                        }
                    ],
                },
                {
                    "id": "detail_app",
                    "title": "应用详情",
                    "purpose": "detail",
                    "layout": "flat",
                    "anchors": ["BackendGroup"],
                    "nodes": ["App1", "App2", "Redis"],
                    "edges": [
                        {
                            "from": "App1",
                            "to": "Redis",
                            "kind": "dependency",
                            "label": "缓存访问",
                        }
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_app partially shows RunCommand dependency Redis" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_reversed_scaling_source_edge():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "ScalingGroup", "type": "ALIYUN::ESS::ScalingGroup"},
                {"id": "SeedEcs", "type": "ALIYUN::ECS::Instance"},
            ],
            "explicit_relations": [],
        },
        {
            "node_labels": [],
            "edges": [
                {
                    "from": "ScalingGroup",
                    "to": "SeedEcs",
                    "kind": "management",
                    "label": "基准实例",
                    "confidence": "high",
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [
                    {
                        "from": "ScalingGroup",
                        "to": "SeedEcs",
                        "kind": "management",
                        "label": "基准实例",
                        "confidence": "high",
                    }
                ],
                "rejected_edges": [],
            }
        },
    )

    assert any("ESS scaling configuration source" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_scaling_configuration_source_edge():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "SeedEcs", "type": "ALIYUN::ECS::Instance"},
                {"id": "ScalingGroup", "type": "ALIYUN::ESS::ScalingGroup"},
                {"id": "ScaledEcs", "type": "CONCEPT::ESS::ScaledECS"},
            ],
            "concept_nodes": [
                {
                    "id": "ScaledEcs",
                    "type": "CONCEPT::ESS::ScaledECS",
                    "source": "SeedEcs",
                    "controller": "ScalingGroup",
                    "via": "ScalingConfiguration",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [{"from": "ScalingGroup", "to": "ScaledEcs", "kind": "management", "label": "弹性伸缩"}],
            "views": [
                {
                    "id": "overview",
                    "nodes": ["ScalingGroup", "ScaledEcs"],
                    "edges": [{"from": "ScalingGroup", "to": "ScaledEcs", "kind": "management", "label": "弹性伸缩"}],
                }
            ],
        },
        {"semantic_plan": {"accepted_node_labels": [], "rejected_node_labels": [], "accepted_edges": []}},
    )

    assert any("ESS scaling configuration source SeedEcs->ScalingGroup" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_repeated_fan_in_edges():
    module = _load_script_module()

    accepted_edges = [
        {"from": "User1", "to": "RoleAggregate", "kind": "management", "label": "绑定角色"},
        {"from": "User2", "to": "RoleAggregate", "kind": "management", "label": "绑定角色"},
        {"from": "User3", "to": "RoleAggregate", "kind": "management", "label": "绑定角色"},
    ]
    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "User1", "type": "ALIYUN::RAM::User"},
                {"id": "User2", "type": "ALIYUN::RAM::User"},
                {"id": "User3", "type": "ALIYUN::RAM::User"},
                {"id": "RoleAggregate", "type": "ALIYUN::RAM::Role"},
            ],
            "explicit_relations": [],
        },
        {"node_labels": [], "edges": accepted_edges},
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("too many repeated edges to RoleAggregate" in issue for issue in issues)


def test_build_llm_architecture_context_prioritizes_ram_governance_scaffold():
    module = _load_script_module()

    context = {
        "target_language": {"code": "zh"},
        "resources": [
            {"id": "User1", "type": "ALIYUN::RAM::User"},
            {"id": "User2", "type": "ALIYUN::RAM::User"},
            {"id": "AccessKey1", "type": "ALIYUN::RAM::AccessKey"},
            {"id": "AccessKey2", "type": "ALIYUN::RAM::AccessKey"},
            {"id": "Group1", "type": "ALIYUN::RAM::Group"},
            {"id": "Group2", "type": "ALIYUN::RAM::Group"},
            {"id": "Role1", "type": "ALIYUN::RAM::Role"},
            {"id": "UserToGroup1", "type": "ALIYUN::RAM::UserToGroupAddition"},
        ],
        "visible_nodes": [
            {"id": "RamUsers", "type": "ALIYUN::RAM::User", "label": "RAM User x2"},
            {"id": "RamGroups", "type": "ALIYUN::RAM::Group", "label": "RAM Group x2"},
            {"id": "RamRoles", "type": "ALIYUN::RAM::Role", "label": "RAM Role"},
            {"id": "OssBuckets", "type": "ALIYUN::OSS::Bucket", "label": "OSS Bucket x3"},
            {"id": "AppServer", "type": "ALIYUN::ECS::Instance", "label": "ECS Instance"},
        ],
    }

    llm_context = module.build_llm_architecture_context(context)

    scaffold_views = llm_context["semantic_plan_scaffold"]["views"]
    assert scaffold_views[1]["id"] == "detail_permissions"
    assert "governance_summary" in llm_context
    assert llm_context["governance_summary"]["primary_intent"] == "identity_and_permission_governance"


def test_validate_semantic_plan_result_rejects_ram_governance_as_data_flow():
    module = _load_script_module()

    accepted_edges = [
        {"from": "AppServer", "to": "Database", "kind": "traffic", "label": "数据库访问"},
        {"from": "AppServer", "to": "OssBuckets", "kind": "inferred", "label": "存储访问"},
        {"from": "RamUsers", "to": "AppServer", "kind": "management", "label": "运维管理"},
    ]
    semantic_plan = {
        "node_labels": [],
        "edges": accepted_edges,
        "views": [
            {
                "id": "overview",
                "title": "多环境应用架构概览",
                "purpose": "展示应用服务器访问数据库和对象存储",
                "layout": "flat",
                "nodes": ["RamUsers", "AppServer", "Database", "OssBuckets"],
                "edges": accepted_edges,
            },
            {
                "id": "detail_data",
                "title": "数据访问详情",
                "purpose": "展开应用数据访问",
                "layout": "flat",
                "anchors": ["AppServer", "Database", "OssBuckets"],
                "nodes": ["AppServer", "Database", "OssBuckets"],
                "edges": accepted_edges[:2],
            },
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "resources": [
                {"id": "User1", "type": "ALIYUN::RAM::User"},
                {"id": "User2", "type": "ALIYUN::RAM::User"},
                {"id": "AccessKey1", "type": "ALIYUN::RAM::AccessKey"},
                {"id": "AccessKey2", "type": "ALIYUN::RAM::AccessKey"},
                {"id": "Group1", "type": "ALIYUN::RAM::Group"},
                {"id": "Group2", "type": "ALIYUN::RAM::Group"},
                {"id": "Role1", "type": "ALIYUN::RAM::Role"},
                {"id": "UserToGroup1", "type": "ALIYUN::RAM::UserToGroupAddition"},
            ],
            "visible_nodes": [
                {"id": "RamUsers", "type": "ALIYUN::RAM::User"},
                {"id": "RamGroups", "type": "ALIYUN::RAM::Group"},
                {"id": "RamRoles", "type": "ALIYUN::RAM::Role"},
                {"id": "OssBuckets", "type": "ALIYUN::OSS::Bucket"},
                {"id": "AppServer", "type": "ALIYUN::ECS::Instance"},
                {"id": "Database", "type": "ALIYUN::RDS::DBInstance"},
            ],
            "explicit_relations": [
                {
                    "source": "AccessKey1",
                    "source_type": "ALIYUN::RAM::AccessKey",
                    "target": "User1",
                    "target_type": "ALIYUN::RAM::User",
                    "property": "UserName",
                },
                {
                    "source": "UserToGroup1",
                    "source_type": "ALIYUN::RAM::UserToGroupAddition",
                    "target": "User1",
                    "target_type": "ALIYUN::RAM::User",
                    "property": "Users",
                },
            ],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("RAM-heavy architecture should include detail_permissions" in issue for issue in issues)
    assert any("RAM-heavy architecture is dominated by application/data-flow views" in issue for issue in issues)


def test_validate_semantic_plan_result_accepts_public_network_drilldown_shape():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [],
        "edges": [
            {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 转发"},
            {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
            {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
        ],
        "views": [
            {
                "id": "overview",
                "title": "多 VPC 互联与流量入口",
                "purpose": "展示公网 NLB 到生产应用的主路径",
                "layout": "contained",
                "groups": [
                    {"id": "ProdAlbGroup", "label": "生产 ALB 集群", "members": ["ProdAlb1", "ProdAlb2"]},
                    {
                        "id": "ProdAppGroup",
                        "label": "生产应用集群",
                        "members": ["App1", "App2", "App3", "App4"],
                    },
                ],
                "nodes": ["DmzNlb", "ProdAlbGroup", "ProdAppGroup"],
                "edges": [
                    {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 转发"},
                    {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                ],
            },
            {
                "id": "detail_app",
                "title": "生产应用负载分发详情",
                "purpose": "展开 ALB 与后端应用服务器",
                "layout": "flat",
                "anchors": ["DmzNlb", "ProdAlbGroup", "ProdAppGroup"],
                "groups": [
                    {
                        "id": "ProdAppGroup",
                        "label": "生产应用集群",
                        "members": ["App1", "App2", "App3", "App4"],
                    }
                ],
                "nodes": ["DmzNlb", "ProdAlb1", "ProdAlb2", "ProdAppGroup"],
                "edges": [
                    {"from": "DmzNlb", "to": "ProdAlb1", "kind": "traffic", "label": "跨 VPC 后端"},
                    {"from": "DmzNlb", "to": "ProdAlb2", "kind": "traffic", "label": "跨 VPC 后端"},
                    {"from": "ProdAlb1", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                    {"from": "ProdAlb2", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                ],
            },
            {
                "id": "detail_network",
                "title": "CEN 跨域网络互联详情",
                "purpose": "展开 CEN 与 DMZ/生产路由域",
                "layout": "flat",
                "anchors": ["DmzNlb", "ProdAlbGroup"],
                "nodes": ["DmzRouteDomain", "ProdRouteDomain1", "ProdRouteDomain2", "TransitRouter"],
                "edges": [
                    {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                    {"from": "ProdRouteDomain1", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                    {"from": "ProdRouteDomain2", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                ],
            },
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "DmzNlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "ProdAlb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "ProdAlb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App1", "type": "ALIYUN::ECS::Instance"},
                {"id": "App2", "type": "ALIYUN::ECS::Instance"},
                {"id": "App3", "type": "ALIYUN::ECS::Instance"},
                {"id": "App4", "type": "ALIYUN::ECS::Instance"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain1", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain2", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc1", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc2", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "DmzNlb", "container": "DmzVpc"},
                {"resource": "ProdAlb1", "container": "ProdVpc1"},
                {"resource": "App1", "container": "ProdVpc1"},
                {"resource": "App2", "container": "ProdVpc1"},
                {"resource": "ProdAlb2", "container": "ProdVpc2"},
                {"resource": "App3", "container": "ProdVpc2"},
                {"resource": "App4", "container": "ProdVpc2"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": semantic_plan["edges"],
                "rejected_edges": [],
            }
        },
    )

    assert issues == []


def test_validate_semantic_plan_result_rejects_public_network_overview_without_main_relationships():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "DmzNlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
                {"id": "ProdAlb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "ProdAlb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App1", "type": "ALIYUN::ECS::Instance"},
                {"id": "App2", "type": "ALIYUN::ECS::Instance"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain1", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain2", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc1", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc2", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "DmzNlb", "container": "DmzVpc"},
                {"resource": "Nat", "container": "DmzVpc"},
                {"resource": "ProdAlb1", "container": "ProdVpc1"},
                {"resource": "App1", "container": "ProdVpc1"},
                {"resource": "ProdAlb2", "container": "ProdVpc2"},
                {"resource": "App2", "container": "ProdVpc2"},
            ],
            "network_attachments": [
                {
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "child_instance_type": "VPC",
                    "child_resource": "DmzVpc",
                },
                {
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "child_instance_type": "VPC",
                    "child_resource": "ProdVpc1",
                },
                {
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "child_instance_type": "VPC",
                    "child_resource": "ProdVpc2",
                },
            ],
            "visible_edges": [],
            "explicit_relations": [],
        },
        {
            "node_labels": [],
            "edges": [
                {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                {"from": "ProdRouteDomain1", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                {"from": "ProdRouteDomain2", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "跨 VPC 应用架构总览",
                    "purpose": "展示主路径",
                    "layout": "contained",
                    "groups": [
                        {"id": "DmzVpcSummary", "label": "DMZ VPC", "members": ["DmzNlb", "Nat"], "parent": "DmzVpc"},
                        {"id": "ProdAlbGroup", "label": "生产 ALB 集群", "members": ["ProdAlb1", "ProdAlb2"]},
                    ],
                    "nodes": ["DmzVpcSummary", "ProdAlbGroup"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "purpose": "展开 CEN 路由",
                    "layout": "flat",
                    "anchors": ["DmzVpcSummary", "ProdAlbGroup"],
                    "nodes": ["DmzVpc", "ProdVpc1", "ProdVpc2", "TransitRouter"],
                    "edges": [
                        {"from": "DmzVpc", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "ProdVpc1", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "ProdVpc2", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view overview has no edges" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_detail_app_to_keep_overview_ingress_source():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [],
        "edges": [
            {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 后端转发"},
            {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
        ],
        "views": [
            {
                "id": "overview",
                "title": "总览",
                "purpose": "展示跨 VPC 入口到生产应用",
                "layout": "contained",
                "groups": [
                    {"id": "ProdAlbGroup", "label": "生产 ALB 集群", "members": ["ProdAlb1", "ProdAlb2"]},
                    {"id": "ProdAppGroup", "label": "生产应用集群", "members": ["App1", "App2"]},
                ],
                "nodes": ["DmzNlb", "ProdAlbGroup", "ProdAppGroup"],
                "edges": [
                    {"from": "DmzNlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 后端转发"},
                    {"from": "ProdAlbGroup", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                ],
            },
            {
                "id": "detail_app",
                "title": "生产应用负载详情",
                "purpose": "展开 ALB 与后端",
                "layout": "flat",
                "anchors": ["ProdAlbGroup", "ProdAppGroup"],
                "groups": [{"id": "ProdAppGroup", "label": "生产应用集群", "members": ["App1", "App2"]}],
                "nodes": ["ProdAlb1", "ProdAlb2", "ProdAppGroup"],
                "edges": [
                    {"from": "ProdAlb1", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                    {"from": "ProdAlb2", "to": "ProdAppGroup", "kind": "traffic", "label": "HTTP 转发"},
                ],
            },
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "DmzNlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "ProdAlb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "ProdAlb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App1", "type": "ALIYUN::ECS::Instance"},
                {"id": "App2", "type": "ALIYUN::ECS::Instance"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc1", "type": "ALIYUN::ECS::VPC"},
                {"id": "ProdVpc2", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "DmzNlb", "container": "DmzVpc"},
                {"resource": "ProdAlb1", "container": "ProdVpc1"},
                {"resource": "App1", "container": "ProdVpc1"},
                {"resource": "ProdAlb2", "container": "ProdVpc2"},
                {"resource": "App2", "container": "ProdVpc2"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": semantic_plan["edges"],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_app should include overview traffic source DmzNlb" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_generic_network_route_domain_labels():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteConfig", "type": "CONCEPT::Layer::AttachmentSummary", "label": "专有网络 VPC 配置"},
                {"id": "ProdRouteConfig1", "type": "CONCEPT::Layer::AttachmentSummary", "label": "专有网络 VPC 配置"},
                {"id": "ProdRouteConfig2", "type": "CONCEPT::Layer::AttachmentSummary", "label": "专有网络 VPC 配置"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
            "explicit_relations": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "purpose": "展开网络配置",
                    "layout": "flat",
                    "anchors": ["TransitRouter"],
                    "nodes": ["TransitRouter", "DmzRouteConfig", "ProdRouteConfig1", "ProdRouteConfig2"],
                    "edges": [
                        {"from": "DmzRouteConfig", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {
                            "from": "ProdRouteConfig1",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        },
                        {
                            "from": "ProdRouteConfig2",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("route/config domain labels should describe purpose" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_purposeful_network_route_domain_labels():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [
            {"id": "SecurityRouteConfig", "label": "安全VPC路由域", "confidence": "high"},
            {"id": "FrontendRouteConfig", "label": "前端VPC路由域", "confidence": "high"},
            {"id": "BackendRouteConfig", "label": "后端VPC路由域", "confidence": "high"},
            {"id": "AccessSubnetRouteConfig", "label": "接入子网路由配置", "confidence": "high"},
            {"id": "ManageSubnetRouteConfig", "label": "管理子网路由配置", "confidence": "high"},
            {"id": "GatewaySubnetRouteConfig", "label": "网关子网路由配置", "confidence": "high"},
        ],
        "edges": [],
        "views": [
            {
                "id": "overview",
                "title": "总览",
                "purpose": "整体架构",
                "layout": "flat",
                "nodes": ["TransitRouter"],
                "edges": [],
            },
            {
                "id": "detail_network",
                "title": "CEN 路由详情",
                "purpose": "展开网络路由配置",
                "layout": "flat",
                "anchors": ["TransitRouter"],
                "nodes": [
                    "TransitRouter",
                    "SecurityRouteConfig",
                    "FrontendRouteConfig",
                    "BackendRouteConfig",
                    "AccessSubnetRouteConfig",
                    "ManageSubnetRouteConfig",
                    "GatewaySubnetRouteConfig",
                ],
                "edges": [
                    {"from": "SecurityRouteConfig", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                    {"from": "FrontendRouteConfig", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                    {"from": "BackendRouteConfig", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                ],
            },
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                *[
                    {"id": node_id, "type": "CONCEPT::Layer::AttachmentSummary", "label": "专有网络 VPC 配置"}
                    for node_id in (
                        "SecurityRouteConfig",
                        "FrontendRouteConfig",
                        "BackendRouteConfig",
                        "AccessSubnetRouteConfig",
                        "ManageSubnetRouteConfig",
                        "GatewaySubnetRouteConfig",
                    )
                ],
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
            "explicit_relations": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": semantic_plan["node_labels"],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("route/config domain labels should describe purpose" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_compact_cen_summary_without_detail_network():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [],
        "edges": [
            {"from": "Bandwidth1", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
            {"from": "Bandwidth2", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
        ],
        "views": [
            {
                "id": "overview",
                "title": "云企业网互联概览",
                "purpose": "展示 CEN 与带宽配置",
                "layout": "contained",
                "nodes": ["layer_CenInstance_Config", "Bandwidth1", "Bandwidth2"],
                "edges": [
                    {
                        "from": "Bandwidth1",
                        "to": "layer_CenInstance_Config",
                        "kind": "dependency",
                        "label": "关联带宽包",
                    },
                    {
                        "from": "Bandwidth2",
                        "to": "layer_CenInstance_Config",
                        "kind": "dependency",
                        "label": "关联带宽包",
                    },
                ],
            }
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Bandwidth1", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "Bandwidth2", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"}],
            "containment": [],
            "visible_edges": [
                {"from": "Bandwidth1", "to": "layer_CenInstance_Config"},
                {"from": "Bandwidth2", "to": "layer_CenInstance_Config"},
            ],
            "network_attachments": [
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC", "child_resource": "Vpc1"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC", "child_resource": "Vpc2"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC", "child_resource": "Vpc3"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VBR", "child_resource": "Vbr1"},
            ],
            "explicit_relations": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": semantic_plan["edges"],
                "rejected_edges": [],
            }
        },
    )

    assert not any("detail_network" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_single_cen_summary_covering_compact_attachments():
    module = _load_script_module()

    semantic_plan = {
        "node_labels": [
            {"id": "layer_CenInstance_Config", "label": "CEN 互联与带宽配置", "confidence": "high"},
        ],
        "edges": [
            {"from": "Bandwidth1", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
            {"from": "Bandwidth2", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "关联带宽包"},
        ],
        "views": [
            {
                "id": "overview",
                "title": "云企业网互联概览",
                "purpose": "展示 CEN 与带宽配置",
                "layout": "contained",
                "nodes": ["layer_CenInstance_Config"],
                "edges": [],
            }
        ],
    }

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Bandwidth1", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "Bandwidth2", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"}],
            "containment": [],
            "visible_edges": [
                {"from": "Bandwidth1", "to": "layer_CenInstance_Config"},
                {"from": "Bandwidth2", "to": "layer_CenInstance_Config"},
            ],
            "attachments": [
                {"marker": "Bandwidth1", "target": "CenInstance"},
                {"marker": "Bandwidth2", "target": "CenInstance"},
            ],
            "network_attachments": [
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VPC"},
                {"type": "ALIYUN::CEN::CenInstanceAttachment", "child_instance_type": "VBR"},
            ],
            "explicit_relations": [],
        },
        semantic_plan,
        {
            "semantic_plan": {
                "accepted_node_labels": semantic_plan["node_labels"],
                "rejected_node_labels": [],
                "accepted_edges": semantic_plan["edges"],
                "rejected_edges": [],
            }
        },
    )

    assert not any("detail_network" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_cen_repeated_vpc_connection_edges():
    module = _load_script_module()

    accepted_edges = [
        {"from": "TransitRouter", "to": "DmzRouteDomain", "kind": "dependency", "label": "VPC 连接"},
        {"from": "TransitRouter", "to": "ProdRouteDomain1", "kind": "dependency", "label": "VPC 连接"},
        {"from": "TransitRouter", "to": "ProdRouteDomain2", "kind": "dependency", "label": "VPC 连接"},
    ]
    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain1", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain2", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "explicit_relations": [],
        },
        {"node_labels": [], "edges": accepted_edges},
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert not any("too many repeated edges from TransitRouter" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_route_config_domain_nodes_in_overview_when_network_detail_exists():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "Route1", "type": "ALIYUN::ECS::Route"},
                {"id": "Route2", "type": "ALIYUN::ECS::Route"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain1", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain2", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter", "DmzRouteDomain", "ProdRouteDomain1", "ProdRouteDomain2"],
                    "edges": [
                        {"from": "TransitRouter", "to": "DmzRouteDomain", "kind": "dependency", "label": "VPC 连接"},
                        {
                            "from": "TransitRouter",
                            "to": "ProdRouteDomain1",
                            "kind": "dependency",
                            "label": "VPC 连接",
                        },
                        {
                            "from": "TransitRouter",
                            "to": "ProdRouteDomain2",
                            "kind": "dependency",
                            "label": "VPC 连接",
                        },
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开网络",
                    "anchors": ["TransitRouter"],
                    "nodes": ["TransitRouter", "DmzRouteDomain"],
                    "edges": [
                        {"from": "TransitRouter", "to": "DmzRouteDomain", "kind": "dependency", "label": "VPC 连接"}
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view overview includes network route/config detail nodes" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_route_config_domain_nodes_in_network_detail():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain1", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain2", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开网络",
                    "anchors": ["TransitRouter"],
                    "nodes": ["TransitRouter", "DmzRouteDomain", "ProdRouteDomain1", "ProdRouteDomain2"],
                    "edges": [
                        {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {
                            "from": "ProdRouteDomain1",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        },
                        {
                            "from": "ProdRouteDomain2",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("view detail_network includes network route/config detail nodes" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_cen_as_overview_vpc_connection_source():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter", "DmzRouteDomain"],
                    "edges": [
                        {
                            "from": "TransitRouter",
                            "to": "DmzRouteDomain",
                            "kind": "dependency",
                            "label": "VPC 互联支撑",
                        }
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("makes CEN/TransitRouter the visual source" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_vpc_domain_to_cen_connection_direction():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter", "DmzRouteDomain"],
                    "edges": [
                        {
                            "from": "DmzRouteDomain",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        }
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("makes CEN/TransitRouter the visual source" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_route_next_hop_compute_on_cen_path():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "ForwarderGroup", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "SubnetRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "VpcRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "network_attachments": [
                {
                    "id": "VpcSecAttachment",
                    "type": "ALIYUN::CEN::TransitRouterVpcAttachment",
                    "network": "CEN",
                    "transit_router": "TransitRouter",
                    "child_instance_type": "VPC",
                    "child_resource": "VpcSec",
                }
            ],
            "explicit_relations": [
                {
                    "source": "RouteForwardToEcs",
                    "target": "ForwarderGroup",
                    "property": "NextHopId",
                    "source_type": "ALIYUN::ECS::Route",
                    "target_type": "ALIYUN::ECS::InstanceGroup",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["TransitRouter", "ForwarderGroup"],
                    "edges": [
                        {
                            "from": "VpcRouteDomain",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        }
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "CEN 和安全转发",
                    "layout": "flat",
                    "anchors": ["TransitRouter", "ForwarderGroup"],
                    "nodes": ["TransitRouter", "ForwarderGroup", "SubnetRouteDomain", "VpcRouteDomain"],
                    "edges": [
                        {
                            "from": "SubnetRouteDomain",
                            "to": "ForwarderGroup",
                            "kind": "dependency",
                            "label": "默认路由",
                        },
                        {
                            "from": "VpcRouteDomain",
                            "to": "TransitRouter",
                            "kind": "dependency",
                            "label": "CEN 接入",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("route next-hop compute ForwarderGroup" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_overview_cen_node_when_business_edge_mentions_cen():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "nodes": ["Nlb", "Alb", "TransitRouter"],
                    "edges": [
                        {"from": "Nlb", "to": "Alb", "kind": "traffic", "label": "经 CEN 后端转发"},
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("repeats CEN/TransitRouter both as an overview node and as a traffic label" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_unknown_view_nodes():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [{"id": "App", "type": "ALIYUN::ECS::Instance"}],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "explicit_relations": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "detail_app",
                    "title": "业务流量",
                    "nodes": ["App"],
                    "edges": [{"from": "App", "to": "MissingNode", "kind": "traffic", "label": "访问"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_app references unknown node MissingNode" in issue for issue in issues)
    assert any("view detail_app edge App->MissingNode references nodes outside the view" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_ack_application_concepts_in_views():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Ack", "type": "ALIYUN::CS::ManagedKubernetesCluster"},
                {"id": "AckApplicationWorkload", "type": "CONCEPT::ACK::ApplicationWorkload"},
                {"id": "AckHpaAutoscaling", "type": "CONCEPT::ACK::HpaAutoscaling"},
            ],
            "containers": [],
            "explicit_relations": [],
            "kubernetes_applications": [{"cluster": "Ack", "source": "AppHpa", "kind": "HorizontalPodAutoscaler"}],
        },
        {
            "node_labels": [],
            "edges": [
                {
                    "from": "AckHpaAutoscaling",
                    "to": "AckApplicationWorkload",
                    "kind": "management",
                    "label": "弹性伸缩",
                    "confidence": "high",
                }
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "flat",
                    "nodes": ["Ack"],
                    "edges": [],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [
                    {
                        "from": "AckHpaAutoscaling",
                        "to": "AckApplicationWorkload",
                        "kind": "management",
                        "label": "弹性伸缩",
                    }
                ],
                "rejected_edges": [],
            }
        },
    )

    assert any("ACK/Kubernetes application views should include CONCEPT::ACK nodes" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_ack_cluster_as_application_data_source():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Ack", "type": "ALIYUN::CS::ManagedKubernetesCluster"},
                {"id": "AckApplicationWorkload", "type": "CONCEPT::ACK::ApplicationWorkload"},
                {"id": "Rds", "type": "ALIYUN::RDS::DBInstance"},
                {"id": "Redis", "type": "ALIYUN::REDIS::Instance"},
            ],
            "containers": [],
            "explicit_relations": [],
            "kubernetes_applications": [
                {"cluster": "Ack", "source": "HelmApp", "kind": "Deployment", "template_refs": ["Rds", "Redis"]}
            ],
        },
        {
            "node_labels": [],
            "edges": [
                {
                    "from": "Ack",
                    "to": "Rds",
                    "kind": "traffic",
                    "label": "数据库访问",
                    "confidence": "high",
                }
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "flat",
                    "nodes": ["Ack", "Rds", "Redis"],
                    "edges": [{"from": "Ack", "to": "Rds", "kind": "traffic", "label": "数据库访问"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [
                    {
                        "from": "Ack",
                        "to": "Rds",
                        "kind": "traffic",
                        "label": "数据库访问",
                    }
                ],
                "rejected_edges": [],
            }
        },
    )

    assert any("ACK/Kubernetes runtime data edges should start from application workload" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_invalid_view_layout():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [{"id": "App", "type": "ALIYUN::ECS::Instance"}],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "展示资源",
                    "layout": "deep",
                    "nodes": ["App"],
                    "edges": [],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view overview has invalid layout deep" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_complex_architecture_without_views():
    module = _load_script_module()

    visible_nodes = [{"id": f"Node{index}", "type": "ALIYUN::ECS::Instance"} for index in range(1, 13)]
    accepted_edges = [
        {"from": f"Node{index}", "to": f"Node{index + 1}", "kind": "traffic", "label": "访问"} for index in range(1, 8)
    ]

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": visible_nodes,
            "concept_nodes": [],
            "explicit_relations": [
                {
                    "source": edge["from"],
                    "target": edge["to"],
                    "source_type": "ALIYUN::ECS::Instance",
                    "target_type": "ALIYUN::ECS::Instance",
                }
                for edge in accepted_edges
            ],
        },
        {"node_labels": [], "edges": accepted_edges},
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("complex architecture should define overview plus drill-down detail views" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_perspective_views_for_complex_architecture():
    module = _load_script_module()

    visible_nodes = [{"id": f"Node{index}", "type": "ALIYUN::ECS::Instance"} for index in range(1, 13)]
    accepted_edges = [
        {"from": f"Node{index}", "to": f"Node{index + 1}", "kind": "traffic", "label": "访问"} for index in range(1, 8)
    ]

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": visible_nodes,
            "containers": [],
            "visible_edges": [],
            "explicit_relations": [
                {
                    "source": edge["from"],
                    "target": edge["to"],
                    "source_type": "ALIYUN::ECS::Instance",
                    "target_type": "ALIYUN::ECS::Instance",
                }
                for edge in accepted_edges
            ],
        },
        {
            "node_labels": [],
            "edges": accepted_edges,
            "views": [
                {
                    "id": "traffic",
                    "title": "业务流量",
                    "purpose": "入口到应用",
                    "nodes": ["Node1", "Node2"],
                    "edges": [{"from": "Node1", "to": "Node2", "kind": "traffic", "label": "访问"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("views must start with overview" in issue for issue in issues)
    assert any("view traffic is a perspective view" in issue for issue in issues)
    assert any("at least one detail_<area>" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_isolated_overview_nodes():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Entry", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
            ],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "flat",
                    "nodes": ["Entry", "App", "Nat"],
                    "edges": [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}],
                "rejected_edges": [],
            }
        },
    )

    assert any("view overview has isolated nodes Nat" in issue for issue in issues)


def test_validate_semantic_plan_result_flags_missing_network_drilldown_for_network_heavy_architecture():
    module = _load_script_module()

    visible_nodes = [
        {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
        {"id": "VpcAttachment1", "type": "ALIYUN::CEN::TransitRouterVpcAttachment"},
        {"id": "VpcAttachment2", "type": "ALIYUN::CEN::TransitRouterVpcAttachment"},
        {"id": "Route1", "type": "ALIYUN::ECS::Route"},
        {"id": "Route2", "type": "ALIYUN::ECS::Route"},
        {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
        {"id": "App1", "type": "ALIYUN::ECS::Instance"},
        {"id": "App2", "type": "ALIYUN::ECS::Instance"},
        {"id": "App3", "type": "ALIYUN::ECS::Instance"},
        {"id": "App4", "type": "ALIYUN::ECS::Instance"},
        {"id": "Alb1", "type": "ALIYUN::ALB::LoadBalancer"},
        {"id": "Alb2", "type": "ALIYUN::ALB::LoadBalancer"},
    ]
    accepted_edges = [
        {"from": "Alb1", "to": "App1", "kind": "traffic", "label": "分发"},
        {"from": "Alb1", "to": "App2", "kind": "traffic", "label": "分发"},
        {"from": "Alb2", "to": "App3", "kind": "traffic", "label": "分发"},
        {"from": "Alb2", "to": "App4", "kind": "traffic", "label": "分发"},
        {"from": "TransitRouter", "to": "VpcAttachment1", "kind": "management", "label": "连接"},
        {"from": "TransitRouter", "to": "VpcAttachment2", "kind": "management", "label": "连接"},
        {"from": "Nat", "to": "Route1", "kind": "management", "label": "出口"},
    ]

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": visible_nodes,
            "containers": [{"id": "Vpc1", "type": "ALIYUN::ECS::VPC"}, {"id": "Vpc2", "type": "ALIYUN::ECS::VPC"}],
            "visible_edges": [],
            "explicit_relations": [
                {
                    "source": edge["from"],
                    "target": edge["to"],
                    "source_type": "ALIYUN::ECS::Instance",
                    "target_type": "ALIYUN::ECS::Instance",
                }
                for edge in accepted_edges
            ],
        },
        {
            "node_labels": [],
            "edges": accepted_edges,
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["TransitRouter", "VpcAttachment1", "VpcAttachment2", "Nat"],
                    "edges": [
                        {"from": "TransitRouter", "to": "VpcAttachment1", "kind": "management", "label": "连接"},
                        {"from": "TransitRouter", "to": "VpcAttachment2", "kind": "management", "label": "连接"},
                        {"from": "Nat", "to": "Route1", "kind": "management", "label": "出口"},
                    ],
                },
                {
                    "id": "detail_app",
                    "title": "应用展开",
                    "purpose": "应用分发",
                    "nodes": ["Alb1", "Alb2", "App1", "App2", "App3", "App4"],
                    "edges": accepted_edges[:4],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("network-heavy architecture should include a detail_network" in issue for issue in issues)


def test_needs_network_drilldown_for_cen_network_attachments():
    module = _load_script_module()

    assert (
        module._needs_network_drilldown_view(
            {
                "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
                "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
                "network_attachments": [
                    {
                        "id": "CurrentVpcAttachment",
                        "type": "ALIYUN::CEN::CenInstanceAttachment",
                        "network": "CEN",
                        "child_instance_type": "VPC",
                        "child_resource": "Vpc",
                    },
                    {
                        "id": "ExternalVbrAttachment",
                        "type": "ALIYUN::CEN::CenInstanceAttachment",
                        "network": "CEN",
                        "child_instance_type": "VBR",
                        "child_instance_id": "Ref:OtherVbrId",
                    },
                ],
            }
        )
        is True
    )


def test_validate_semantic_plan_result_rejects_contained_network_detail_that_expands_application_container():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "VSwitch1", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
                {"id": "VSwitch2", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            ],
            "containment": [
                {"resource": "BackendServer1", "container": "VSwitch1"},
                {"resource": "BackendServer2", "container": "VSwitch2"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "CenVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开 CEN 接入",
                    "layout": "contained",
                    "anchors": ["LoadBalancer"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "management", "label": "CEN 接入"}
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_network uses contained network containers Vpc" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_cen_detail_to_include_attached_child_resource():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "containment": [
                {"resource": "BackendServer1", "container": "Vpc"},
                {"resource": "BackendServer2", "container": "Vpc"},
            ],
            "visible_edges": [{"from": "CenBandwidthPackage", "to": "layer_CenInstance_Config"}],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "TheCenVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                    "child_resource_type": "ALIYUN::ECS::VPC",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "purpose": "展示 CEN 接入",
                    "layout": "flat",
                    "anchors": ["BackendGroup"],
                    "nodes": ["CenBandwidthPackage", "layer_CenInstance_Config"],
                    "edges": [
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "绑定带宽包",
                        }
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_network should include CEN attached resources Vpc" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_cen_detail_to_explain_external_networks():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "CenBandwidthPackage", "type": "ALIYUN::CEN::CenBandwidthPackage"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "containment": [
                {"resource": "BackendServer1", "container": "Vpc"},
                {"resource": "BackendServer2", "container": "Vpc"},
            ],
            "visible_edges": [{"from": "CenBandwidthPackage", "to": "layer_CenInstance_Config"}],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "ExternalVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_instance_id": "Ref:OtherVpcId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联配置", "confidence": "high"}],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "purpose": "展示 CEN 接入",
                    "layout": "flat",
                    "anchors": ["BackendGroup"],
                    "nodes": ["Vpc", "CenBandwidthPackage", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"},
                        {
                            "from": "CenBandwidthPackage",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "带宽包绑定",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联配置"}],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_network should explain external CEN networks" in issue for issue in issues)


def test_validate_semantic_plan_result_accepts_cen_detail_that_names_external_networks():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "containment": [
                {"resource": "BackendServer1", "container": "Vpc"},
                {"resource": "BackendServer2", "container": "Vpc"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联（外部 VBR）", "confidence": "high"}],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "layout": "flat",
                    "anchors": ["BackendGroup"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {
                            "from": "Vpc",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "与外部网络互联",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [{"id": "layer_CenInstance_Config", "label": "CEN 互联（外部 VBR）"}],
                "rejected_node_labels": [],
                "accepted_edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发"}],
                "rejected_edges": [],
            }
        },
    )

    assert not any("view detail_network should explain external CEN networks" in issue for issue in issues)


def test_validate_semantic_plan_result_accepts_external_cen_edge_as_cross_network_label():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [{"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"}],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "CenInstance", "type": "ALIYUN::CEN::CenInstance"},
            ],
            "containment": [{"resource": "layer_CenInstance_Config", "container": "CenInstance"}],
            "visible_edges": [],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "ExternalVbrAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VBR",
                    "child_instance_id": "Ref:OtherVBRId",
                },
                {
                    "id": "CurrentVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                },
            ],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "nodes": ["Vpc"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 互联详情",
                    "layout": "flat",
                    "anchors": ["Vpc"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {
                            "from": "Vpc",
                            "to": "layer_CenInstance_Config",
                            "kind": "dependency",
                            "label": "连接外部VPC/VBR",
                        },
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("crosses VPCs and should mention CEN or cross-VPC" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_group_anchor_when_parent_container_is_selected():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
                {"id": "layer_CenInstance_Config", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "containment": [
                {"resource": "BackendServer1", "container": "Vpc"},
                {"resource": "BackendServer2", "container": "Vpc"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
            "network_attachments": [
                {
                    "id": "TheCenVpcAttachment",
                    "type": "ALIYUN::CEN::CenInstanceAttachment",
                    "network": "CEN",
                    "child_instance_type": "VPC",
                    "child_resource": "Vpc",
                }
            ],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["BackendGroup"],
                    "edges": [],
                },
                {
                    "id": "detail_network",
                    "layout": "flat",
                    "anchors": ["BackendGroup"],
                    "nodes": ["Vpc", "layer_CenInstance_Config"],
                    "edges": [
                        {"from": "Vpc", "to": "layer_CenInstance_Config", "kind": "dependency", "label": "CEN 接入"}
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any(
        "view detail_network should include anchored network domains BackendGroup" in issue for issue in issues
    )


def test_validate_semantic_plan_result_allows_overview_container_boundary_for_connected_group():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "LoadBalancer", "type": "ALIYUN::SLB::LoadBalancer"},
                {"id": "BackendServer1", "type": "ALIYUN::ECS::Instance"},
                {"id": "BackendServer2", "type": "ALIYUN::ECS::Instance"},
            ],
            "containers": [{"id": "Vpc", "type": "ALIYUN::ECS::VPC"}],
            "containment": [
                {"resource": "BackendServer1", "container": "Vpc"},
                {"resource": "BackendServer2", "container": "Vpc"},
            ],
            "visible_edges": [],
            "explicit_relations": [],
        },
        {
            "node_labels": [],
            "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["LoadBalancer", "BackendGroup", "Vpc"],
                    "edges": [{"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [
                    {"from": "LoadBalancer", "to": "BackendGroup", "kind": "traffic", "label": "转发请求"}
                ],
                "rejected_edges": [],
            }
        },
    )

    assert not any("view overview has isolated nodes Vpc" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_detail_view_anchors():
    module = _load_script_module()

    accepted_edges = [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}]
    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Entry", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            ],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": accepted_edges,
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["Entry", "App"],
                    "edges": accepted_edges,
                },
                {
                    "id": "detail_app",
                    "title": "应用展开",
                    "purpose": "展开应用",
                    "nodes": ["Entry", "App"],
                    "edges": accepted_edges,
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_app must include anchors" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_detail_anchors_to_exist_in_overview():
    module = _load_script_module()

    accepted_edges = [{"from": "Entry", "to": "App", "kind": "traffic", "label": "转发"}]
    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Entry", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "Db", "type": "ALIYUN::RDS::DBInstance"},
            ],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": accepted_edges,
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["Entry", "App"],
                    "edges": accepted_edges,
                },
                {
                    "id": "detail_app",
                    "title": "应用展开",
                    "purpose": "展开应用",
                    "anchors": ["Db"],
                    "nodes": ["App", "Db"],
                    "edges": [{"from": "App", "to": "Db", "kind": "traffic", "label": "访问"}],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": accepted_edges,
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_app anchor Db is not present in overview" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_overview_summary_group_anchor():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Alb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "Alb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "App", "type": "ALIYUN::ECS::InstanceGroup"},
            ],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "groups": [{"id": "AlbGroup", "label": "生产 ALB", "members": ["Alb1", "Alb2"]}],
                    "nodes": ["AlbGroup", "App"],
                    "edges": [{"from": "AlbGroup", "to": "App", "kind": "traffic", "label": "分发流量"}],
                },
                {
                    "id": "detail_app",
                    "title": "应用展开",
                    "purpose": "展开应用",
                    "anchors": ["AlbGroup"],
                    "nodes": ["Alb1", "Alb2", "App"],
                    "edges": [
                        {"from": "Alb1", "to": "App", "kind": "traffic", "label": "分发流量"},
                        {"from": "Alb2", "to": "App", "kind": "traffic", "label": "分发流量"},
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any("AlbGroup" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_unknown_summary_group_members():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [{"id": "Alb1", "type": "ALIYUN::ALB::LoadBalancer"}],
            "containers": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "groups": [{"id": "AlbGroup", "label": "生产 ALB", "members": ["Alb1", "MissingAlb"]}],
                    "nodes": ["AlbGroup"],
                    "edges": [],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view overview group AlbGroup references unknown member MissingAlb" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_cross_vpc_load_balancer_edge_label():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "PrdVpc", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "Nlb", "container": "DmzVpc"},
                {"resource": "Alb", "container": "PrdVpc"},
            ],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [{"from": "Nlb", "to": "Alb", "kind": "traffic", "label": "后端转发"}],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["Nlb", "Alb"],
                    "edges": [{"from": "Nlb", "to": "Alb", "kind": "traffic", "label": "后端转发"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("crosses VPCs and should mention CEN or cross-VPC" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_overview_direct_vpc_edge_when_cen_is_present():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "PrdVpc", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "Nlb", "container": "DmzVpc"},
                {"resource": "Alb", "container": "PrdVpc"},
            ],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "groups": [
                        {"id": "DmzGroup", "label": "DMZ VPC", "members": ["Nlb"], "parent": "DmzVpc"},
                        {"id": "PrdGroup", "label": "生产 VPC", "members": ["Alb"], "parent": "PrdVpc"},
                    ],
                    "nodes": ["DmzGroup", "PrdGroup", "TransitRouter"],
                    "edges": [
                        {"from": "DmzGroup", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "PrdGroup", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "DmzGroup", "to": "PrdGroup", "kind": "traffic", "label": "NLB 后端转发"},
                    ],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("overview edge DmzGroup->PrdGroup crosses VPCs; route it through CEN" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_load_balancer_to_cen_business_edge():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["Nlb", "TransitRouter"],
                    "edges": [{"from": "Nlb", "to": "TransitRouter", "kind": "traffic", "label": "入口流量"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("uses CEN/TransitRouter as a business traffic endpoint" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_nat_to_load_balancer_public_access_edge():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "NatGateway", "type": "ALIYUN::VPC::NatGateway"},
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["NatGateway", "Nlb"],
                    "edges": [{"from": "NatGateway", "to": "Nlb", "kind": "management", "label": "公网访问"}],
                }
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("NAT/SNAT should not be drawn as load balancer ingress" in issue for issue in issues)


def test_validate_semantic_plan_result_rejects_cen_vpc_connection_to_load_balancer_in_network_detail():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            ],
            "containers": [
                {"id": "PrdVpc", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "Alb", "container": "PrdVpc"},
            ],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "nodes": ["TransitRouter", "Alb"],
                    "edges": [{"from": "TransitRouter", "to": "Alb", "kind": "dependency", "label": "生产 VPC 连接"}],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开 CEN",
                    "anchors": ["TransitRouter", "Alb"],
                    "nodes": ["TransitRouter", "Alb"],
                    "edges": [{"from": "TransitRouter", "to": "Alb", "kind": "dependency", "label": "生产 VPC 连接"}],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("connects CEN/TransitRouter to a load balancer" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_network_detail_to_show_anchored_domains():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "RouteConfig", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb", "type": "ALIYUN::ALB::LoadBalancer"},
            ],
            "containers": [
                {"id": "DmzVpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "PrdVpc", "type": "ALIYUN::ECS::VPC"},
            ],
            "containment": [
                {"resource": "Nlb", "container": "DmzVpc"},
                {"resource": "Nat", "container": "DmzVpc"},
                {"resource": "Alb", "container": "PrdVpc"},
            ],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "groups": [
                        {"id": "DmzGroup", "label": "DMZ VPC", "members": ["Nlb", "Nat"], "parent": "DmzVpc"},
                        {"id": "PrdGroup", "label": "生产 VPC", "members": ["Alb"], "parent": "PrdVpc"},
                    ],
                    "nodes": ["DmzGroup", "PrdGroup", "TransitRouter"],
                    "edges": [
                        {"from": "DmzGroup", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "PrdGroup", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开网络",
                    "anchors": ["DmzGroup", "PrdGroup", "TransitRouter"],
                    "nodes": ["RouteConfig", "TransitRouter", "Nat"],
                    "edges": [
                        {"from": "RouteConfig", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                        {"from": "Nat", "to": "RouteConfig", "kind": "management", "label": "SNAT 路由"},
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert any("view detail_network should include anchored network domains" in issue for issue in issues)


def test_validate_semantic_plan_result_requires_multi_vswitch_detail_to_show_network_domains():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "BareMetalGroup", "type": "ALIYUN::ECS::InstanceGroup"},
                {"id": "VCenter", "type": "ALIYUN::ECS::Instance"},
                {"id": "Nat", "type": "ALIYUN::VPC::NatGateway"},
            ],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "ManagementVSwitch", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
                {"id": "OverlayVSwitch", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
                {"id": "ExternalVSwitch", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            ],
            "containment": [
                {"resource": "BareMetalGroup", "container": "ExternalVSwitch"},
                {"resource": "VCenter", "container": "ManagementVSwitch"},
                {"resource": "Nat", "container": "ExternalVSwitch"},
            ],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [
                {"from": "VCenter", "to": "BareMetalGroup", "kind": "management", "label": "VMware 管理"},
                {"from": "Nat", "to": "BareMetalGroup", "kind": "traffic", "label": "SNAT 出网"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "VMware 上云架构概览",
                    "layout": "contained",
                    "nodes": ["BareMetalGroup", "VCenter", "Nat"],
                    "edges": [
                        {"from": "VCenter", "to": "BareMetalGroup", "kind": "management", "label": "VMware 管理"},
                        {"from": "Nat", "to": "BareMetalGroup", "kind": "traffic", "label": "SNAT 出网"},
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "多交换机网络分区详情",
                    "purpose": "展开裸金属实例的多网卡与不同业务子网的连接关系",
                    "anchors": ["BareMetalGroup"],
                    "nodes": ["BareMetalGroup"],
                    "edges": [],
                },
            ],
        },
        {"semantic_plan": {"accepted_node_labels": [], "rejected_node_labels": [], "accepted_edges": []}},
    )

    assert any("view detail_network should include VSwitch/network domains" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_multi_vswitch_detail_with_route_domain_concepts():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "DmzRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "ProdRouteDomain", "type": "CONCEPT::Layer::AttachmentSummary"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
            ],
            "containers": [
                {"id": "Vpc", "type": "ALIYUN::ECS::VPC"},
                {"id": "VSwitch1", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
                {"id": "VSwitch2", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
                {"id": "VSwitch3", "type": "ALIYUN::ECS::VSwitch", "parent": "Vpc"},
            ],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [
                {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                {"from": "ProdRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "概览",
                    "layout": "contained",
                    "nodes": ["DmzRouteDomain", "ProdRouteDomain", "TransitRouter"],
                    "edges": [
                        {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                        {
                            "from": "ProdRouteDomain",
                            "to": "TransitRouter",
                            "kind": "management",
                            "label": "CEN 接入",
                        },
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "CEN 路由详情",
                    "purpose": "展开 DMZ 和生产路由域",
                    "anchors": ["DmzRouteDomain", "ProdRouteDomain"],
                    "nodes": ["DmzRouteDomain", "ProdRouteDomain", "TransitRouter"],
                    "edges": [
                        {"from": "DmzRouteDomain", "to": "TransitRouter", "kind": "management", "label": "CEN 接入"},
                        {
                            "from": "ProdRouteDomain",
                            "to": "TransitRouter",
                            "kind": "management",
                            "label": "CEN 接入",
                        },
                    ],
                },
            ],
        },
        {"semantic_plan": {"accepted_node_labels": [], "rejected_node_labels": [], "accepted_edges": []}},
    )

    assert not any("view detail_network should include VSwitch/network domains" in issue for issue in issues)


def test_validate_semantic_plan_result_allows_detail_network_anchor_to_business_summary_group():
    module = _load_script_module()

    issues = module.validate_semantic_plan_result(
        {
            "target_language": {"code": "zh"},
            "visible_nodes": [
                {"id": "Nlb", "type": "ALIYUN::NLB::LoadBalancer"},
                {"id": "Alb1", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "Alb2", "type": "ALIYUN::ALB::LoadBalancer"},
                {"id": "TransitRouter", "type": "ALIYUN::CEN::TransitRouter"},
                {"id": "RouteConfig", "type": "CONCEPT::Layer::AttachmentSummary"},
            ],
            "containers": [],
            "containment": [],
            "visible_edges": [],
        },
        {
            "node_labels": [],
            "edges": [],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "整体架构",
                    "layout": "contained",
                    "groups": [{"id": "ProdAlbGroup", "label": "生产 ALB 集群", "members": ["Alb1", "Alb2"]}],
                    "nodes": ["Nlb", "ProdAlbGroup"],
                    "edges": [{"from": "Nlb", "to": "ProdAlbGroup", "kind": "traffic", "label": "经 CEN 转发"}],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "purpose": "展开网络",
                    "anchors": ["Nlb", "ProdAlbGroup"],
                    "nodes": ["TransitRouter", "RouteConfig"],
                    "edges": [
                        {"from": "RouteConfig", "to": "TransitRouter", "kind": "dependency", "label": "CEN 接入"},
                    ],
                },
            ],
        },
        {
            "semantic_plan": {
                "accepted_node_labels": [],
                "rejected_node_labels": [],
                "accepted_edges": [],
                "rejected_edges": [],
            }
        },
    )

    assert not any(
        "view detail_network should include anchored network domains ProdAlbGroup" in issue for issue in issues
    )


def test_browser_mermaid_source_quotes_subgraph_labels_for_mermaid_v11():
    module = _load_script_module()

    result = module.browser_mermaid_source(
        """graph TD
  subgraph layer_VPC [VPC (192.168.0.0/16)]
    ECS["ECS"]
  end
"""
    )

    assert 'subgraph layer_VPC["VPC (192.168.0.0/16)"]' in result


def test_parse_args_accepts_terminal_asset_outputs(tmp_path):
    module = _load_script_module()

    args = module.parse_args(
        [
            "template.yml",
            "--terminal-svg-out",
            str(tmp_path / "diagram.svg"),
            "--terminal-png-out",
            str(tmp_path / "diagram.png"),
        ]
    )

    assert args.terminal_svg_out == tmp_path / "diagram.svg"
    assert args.terminal_png_out == tmp_path / "diagram.png"


def test_parse_args_accepts_terminal_recording_outputs(tmp_path):
    module = _load_script_module()

    args = module.parse_args(
        [
            "template.yml",
            "--record-terminal-png-out",
            str(tmp_path / "diagram.png"),
            "--record-terminal-gif-out",
            str(tmp_path / "diagram.gif"),
            "--record-terminal-cast-out",
            str(tmp_path / "diagram.cast"),
            "--record-terminal-cols",
            "220",
            "--record-terminal-rows",
            "140",
        ]
    )

    assert args.record_terminal_png_out == tmp_path / "diagram.png"
    assert args.record_terminal_gif_out == tmp_path / "diagram.gif"
    assert args.record_terminal_cast_out == tmp_path / "diagram.cast"
    assert args.record_terminal_cols == 220
    assert args.record_terminal_rows == 140


def test_parse_args_accepts_multi_view_output_dirs(tmp_path):
    module = _load_script_module()

    args = module.parse_args(
        [
            "template.yml",
            "--view-mermaid-dir",
            str(tmp_path / "mmd"),
            "--terminal-svg-dir",
            str(tmp_path / "svg"),
            "--terminal-png-dir",
            str(tmp_path / "png"),
        ]
    )

    assert args.view_mermaid_dir == tmp_path / "mmd"
    assert args.terminal_svg_dir == tmp_path / "svg"
    assert args.terminal_png_dir == tmp_path / "png"


def test_parse_args_accepts_quiet_terminal_preview_mode():
    module = _load_script_module()

    args = module.parse_args(["template.yml", "--quiet"])

    assert args.quiet is True


def test_parse_args_disables_thinking_by_default_but_allows_opt_in():
    module = _load_script_module()

    default_args = module.parse_args(["template.yml"])
    thinking_args = module.parse_args(["template.yml", "--enable-thinking"])

    assert default_args.enable_thinking is False
    assert thinking_args.enable_thinking is True


def test_format_timing_summary_reports_key_stages():
    module = _load_script_module()

    summary = module.format_timing_summary(
        {
            "load_and_facts": 0.05,
            "llm": 123.45,
            "validate": 0.1,
            "write_outputs": 0.2,
            "terminal_preview": 1.2,
            "total": 125.0,
        }
    )

    assert summary == (
        "timing: load/facts=50ms, llm=123.5s, validate/render=100ms, "
        "write outputs=200ms, terminal preview=1.2s, total=125.0s"
    )


def test_svg_to_png_command_prefers_available_converter(monkeypatch, tmp_path):
    module = _load_script_module()
    svg_path = tmp_path / "diagram.svg"
    png_path = tmp_path / "diagram.png"

    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda command: "/usr/bin/rsvg-convert" if command == "rsvg-convert" else None,
    )

    assert module._svg_to_png_command(svg_path, png_path) == ["rsvg-convert", str(svg_path), "-o", str(png_path)]


def test_write_terminal_svg_records_without_stdout(monkeypatch, tmp_path, capsys):
    module = _load_script_module()

    calls = []
    monkeypatch.setattr(module, "_render_terminal_rich", lambda source: calls.append(source) or "diagram")
    svg_path = tmp_path / "diagram.svg"

    module.write_terminal_svg(svg_path, "graph TD\n  A-->B", width=80, title="diagram")

    assert svg_path.exists()
    assert calls == ["graph TD\n  A-->B"]
    assert "diagram" not in capsys.readouterr().out


def test_convert_svg_to_png_suppresses_converter_output(monkeypatch, tmp_path):
    module = _load_script_module()
    svg_path = tmp_path / "diagram.svg"
    png_path = tmp_path / "diagram.png"
    calls = []

    monkeypatch.setattr(module, "_svg_to_png_command", lambda _svg, _png: ["converter", str(svg_path), str(png_path)])
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    module.convert_svg_to_png(svg_path, png_path)

    assert calls
    assert calls[0][1]["stdout"] is module.subprocess.PIPE
    assert calls[0][1]["stderr"] is module.subprocess.PIPE


def test_svg_to_png_command_supports_macos_sips(monkeypatch, tmp_path):
    module = _load_script_module()
    svg_path = tmp_path / "diagram.svg"
    png_path = tmp_path / "diagram.png"

    monkeypatch.setattr(module.shutil, "which", lambda command: "/usr/bin/sips" if command == "sips" else None)

    assert module._svg_to_png_command(svg_path, png_path) == [
        "sips",
        "-s",
        "format",
        "png",
        str(svg_path),
        "--out",
        str(png_path),
    ]


def test_record_terminal_preview_requires_asciinema(monkeypatch, tmp_path):
    module = _load_script_module()
    args = module.parse_args(
        [
            "template.yml",
            "--record-terminal-png-out",
            str(tmp_path / "diagram.png"),
        ]
    )

    monkeypatch.setattr(module.shutil, "which", lambda _command: None)

    with pytest.raises(RuntimeError, match="asciinema"):
        module.record_terminal_preview(args)


def test_record_terminal_preview_runs_asciinema_agg_and_ffmpeg(monkeypatch, tmp_path):
    module = _load_script_module()
    png_path = tmp_path / "diagram.png"
    gif_path = tmp_path / "diagram.gif"
    cast_path = tmp_path / "diagram.cast"
    args = module.parse_args(
        [
            "template.yml",
            "--model",
            "qwen-test",
            "--max-attempts",
            "2",
            "--record-terminal-png-out",
            str(png_path),
            "--record-terminal-gif-out",
            str(gif_path),
            "--record-terminal-cast-out",
            str(cast_path),
            "--record-terminal-cols",
            "200",
            "--record-terminal-rows",
            "120",
        ]
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda command: f"/usr/bin/{command}" if command in {"asciinema", "agg", "ffmpeg"} else None,
    )
    monkeypatch.setattr(module.subprocess, "run", lambda command, **_kwargs: calls.append(command))

    module.record_terminal_preview(args)

    assert calls[0][:7] == [
        "asciinema",
        "rec",
        "-q",
        "--overwrite",
        "--headless",
        "--return",
        "--window-size",
    ]
    assert calls[0][7] == "200x120"
    assert calls[0][-1] == str(cast_path)
    child_command = calls[0][calls[0].index("-c") + 1]
    assert "preview_template_architecture_llm.py" in child_command
    assert "--quiet" in child_command
    assert "--width 200" in child_command
    assert "--model qwen-test" in child_command
    assert calls[1] == [
        "agg",
        "-q",
        "--cols",
        "200",
        "--rows",
        "120",
        "--font-size",
        "16",
        "--line-height",
        "1.25",
        "--theme",
        "github-dark",
        "--select",
        "100%",
        str(cast_path),
        str(gif_path),
    ]
    assert calls[2] == [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(gif_path),
        "-frames:v",
        "1",
        str(png_path),
    ]


def test_record_terminal_child_command_preserves_semantic_output_paths(tmp_path):
    module = _load_script_module()
    args = module.parse_args(
        [
            "template.yml",
            "--mermaid-out",
            str(tmp_path / "diagram.mmd"),
            "--plan-out",
            str(tmp_path / "plan.json"),
            "--view-mermaid-dir",
            str(tmp_path / "views"),
            "--prompt-debug-html-out",
            str(tmp_path / "prompts.html"),
            "--record-terminal-png-out",
            str(tmp_path / "terminal.png"),
            "--record-terminal-gif-out",
            str(tmp_path / "terminal.gif"),
            "--record-terminal-cast-out",
            str(tmp_path / "terminal.cast"),
        ]
    )

    command = module._record_terminal_child_command(args, width=220)

    assert "--mermaid-out" in command
    assert str(tmp_path / "diagram.mmd") in command
    assert "--plan-out" in command
    assert str(tmp_path / "plan.json") in command
    assert "--view-mermaid-dir" in command
    assert str(tmp_path / "views") in command
    assert "--prompt-debug-html-out" in command
    assert str(tmp_path / "prompts.html") in command
    assert "--record-terminal-png-out" not in command
    assert "--record-terminal-gif-out" not in command
    assert "--record-terminal-cast-out" not in command


def test_terminal_preview_items_use_single_semantic_view_instead_of_base_diagram():
    module = _load_script_module()

    items = module._terminal_preview_items(
        "graph TD\n  Base[old base diagram]",
        SimpleNamespace(
            views=(
                SimpleNamespace(
                    id="overview",
                    title="架构概览",
                    mermaid_source="graph TD\n  Overview[new view diagram]",
                ),
            )
        ),
    )

    assert [(item.id, item.title, item.mermaid_source) for item in items] == [
        ("overview", "架构概览", "graph TD\n  Overview[new view diagram]")
    ]
