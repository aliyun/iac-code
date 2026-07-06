from __future__ import annotations

import json

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    RosResourceTypeDetail,
    build_resource_inventory_snapshot,
)
from iac_code.pipeline.engine.architecture_rule_drafts import (
    apply_reviewed_draft_patches,
    parse_draft_rules_response,
    render_draft_review_report_markdown,
    review_valid_draft_rules,
    validate_draft_rules,
)
from iac_code.pipeline.engine.architecture_rule_facts import build_resource_rule_facts


def _repo() -> ArchitectureMetaRepository:
    def prop(name: str, target: str) -> dict:
        return {"ROS": name, "RelatedTo": [{"ResourceType": f"ROS/{target}"}]}

    return ArchitectureMetaRepository.from_raw(
        categories=[{"CategoryCode": "network", "ProductCodes": ["ecs", "vpc", "nas", "ess"]}],
        products=[
            {"ProductCode": "ecs", "Name": {"en": "ECS", "zh": "云服务器"}, "RelevantCodes": {"ROS": "ECS"}},
            {"ProductCode": "vpc", "Name": {"en": "VPC", "zh": "专有网络"}, "RelevantCodes": {"ROS": "VPC"}},
            {"ProductCode": "nas", "Name": {"en": "NAS", "zh": "文件存储"}, "RelevantCodes": {"ROS": "NAS"}},
            {"ProductCode": "ess", "Name": {"en": "ESS", "zh": "弹性伸缩"}, "RelevantCodes": {"ROS": "ESS"}},
        ],
        config=[
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::VPC"},
                "ProductCode": "ecs",
                "Name": {"en": "VPC", "zh": "专有网络 VPC"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::VSwitch"},
                "ProductCode": "ecs",
                "Name": {"en": "VSwitch", "zh": "交换机"},
                "Properties": [prop("VpcId", "ALIYUN::ECS::VPC")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::SecurityGroup"},
                "ProductCode": "ecs",
                "Name": {"en": "Security Group", "zh": "安全组"},
                "Properties": [prop("VpcId", "ALIYUN::ECS::VPC")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::Instance"},
                "ProductCode": "ecs",
                "Name": {"en": "ECS Instance", "zh": "ECS 实例"},
                "Properties": [
                    prop("VSwitchId", "ALIYUN::ECS::VSwitch"),
                    prop("SecurityGroupId", "ALIYUN::ECS::SecurityGroup"),
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::NatGateway"},
                "ProductCode": "vpc",
                "Name": {"en": "NAT Gateway", "zh": "NAT 网关"},
                "Properties": [prop("VpcId", "ALIYUN::ECS::VPC")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::SnatEntry"},
                "ProductCode": "vpc",
                "Name": {"en": "SNAT Entry", "zh": "SNAT 条目"},
                "Properties": [prop("SnatTableId", "ALIYUN::VPC::NatGateway")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::CommonBandwidthPackage"},
                "ProductCode": "vpc",
                "Name": {"en": "Shared Bandwidth Package", "zh": "共享带宽包"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::EIP"},
                "ProductCode": "vpc",
                "Name": {"en": "EIP", "zh": "弹性公网 IP"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::CommonBandwidthPackageIp"},
                "ProductCode": "vpc",
                "Name": {"en": "Shared Bandwidth IP", "zh": "共享带宽 IP"},
                "Properties": [
                    prop("BandwidthPackageId", "ALIYUN::VPC::CommonBandwidthPackage"),
                    prop("Eips", "ALIYUN::VPC::EIP"),
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::NAS::FileSystem"},
                "ProductCode": "nas",
                "Name": {"en": "NAS File System", "zh": "NAS 文件系统"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::NAS::MountTarget"},
                "ProductCode": "nas",
                "Name": {"en": "NAS Mount Target", "zh": "NAS 挂载点"},
                "Properties": [prop("FileSystemId", "ALIYUN::NAS::FileSystem")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::RunCommand"},
                "ProductCode": "ecs",
                "Name": {"en": "Run Command", "zh": "执行命令"},
                "Properties": [prop("InstanceIds", "ALIYUN::ECS::Instance")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ESS::ScalingConfiguration"},
                "ProductCode": "ess",
                "Name": {"en": "Scaling Configuration", "zh": "伸缩配置"},
                "Properties": [
                    prop("ScalingGroupId", "ALIYUN::ESS::ScalingGroup"),
                    prop("InstanceId", "ALIYUN::ECS::Instance"),
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ESS::ScalingGroup"},
                "ProductCode": "ess",
                "Name": {"en": "Scaling Group", "zh": "伸缩组"},
                "Properties": [prop("VSwitchId", "ALIYUN::ECS::VSwitch")],
            },
        ],
    )


def _detail(resource_type: str, description: str = "", properties: dict | None = None) -> RosResourceTypeDetail:
    return RosResourceTypeDetail(
        resource_type=resource_type,
        entity_type="Resource",
        provider="ROS",
        properties=properties or {},
        attributes={},
        description=description,
    )


def _facts():
    snapshot = build_resource_inventory_snapshot(
        api_resource_types=[
            "ALIYUN::ECS::VPC",
            "ALIYUN::ECS::VSwitch",
            "ALIYUN::ECS::SecurityGroup",
            "ALIYUN::ECS::Instance",
            "ALIYUN::VPC::NatGateway",
            "ALIYUN::VPC::SnatEntry",
            "ALIYUN::VPC::CommonBandwidthPackage",
            "ALIYUN::VPC::EIP",
            "ALIYUN::VPC::CommonBandwidthPackageIp",
            "ALIYUN::NAS::FileSystem",
            "ALIYUN::NAS::MountTarget",
            "ALIYUN::ECS::RunCommand",
            "ALIYUN::ESS::ScalingConfiguration",
            "ALIYUN::ESS::ScalingGroup",
        ],
        details_by_type={
            "ALIYUN::VPC::SnatEntry": _detail(
                "ALIYUN::VPC::SnatEntry",
                "SNAT entries configure internet access through a NAT gateway.",
                {"SnatTableId": {"Description": "The ID of the SNAT table."}},
            ),
            "ALIYUN::VPC::CommonBandwidthPackageIp": _detail(
                "ALIYUN::VPC::CommonBandwidthPackageIp",
                "Associates EIPs with a shared bandwidth package.",
            ),
            "ALIYUN::NAS::MountTarget": _detail(
                "ALIYUN::NAS::MountTarget",
                "Mount point used by compute resources to access a NAS file system.",
            ),
            "ALIYUN::ECS::RunCommand": _detail(
                "ALIYUN::ECS::RunCommand",
                "Runs a Cloud Assistant command on ECS instances.",
                {"CommandContent": {"Description": "The command content."}},
            ),
            "ALIYUN::ESS::ScalingConfiguration": _detail(
                "ALIYUN::ESS::ScalingConfiguration",
                "Defines ECS instance settings for a scaling group.",
            ),
        },
        meta_repository=_repo(),
    )
    return build_resource_rule_facts(snapshot)


def _draft_payload() -> str:
    return json.dumps(
        {
            "draft_rules": [
                {
                    "resource_type": "ALIYUN::VPC::SnatEntry",
                    "classification": "attachment",
                    "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                    "property_names": ["SnatTableId"],
                    "label": {"zh": "SNAT条目", "en": "SNAT entry"},
                    "edge_label": None,
                    "confidence": 0.91,
                    "evidence": ["Description says SNAT entries configure internet access through NAT Gateway."],
                    "suggested_architecture_rules_patch": {
                        "compact_child_attachments": [
                            {
                                "resource_types": ["ALIYUN::VPC::SnatEntry"],
                                "target_properties": ["SnatTableId"],
                                "target_types": ["ALIYUN::VPC::NatGateway"],
                                "label": {"zh": "SNAT条目", "en": "SNAT entry"},
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::VPC::CommonBandwidthPackageIp",
                    "classification": "attachment_edge",
                    "target_resource_types": [
                        "ALIYUN::VPC::CommonBandwidthPackage",
                        "ALIYUN::VPC::EIP",
                    ],
                    "property_names": ["BandwidthPackageId", "Eips"],
                    "label": {"zh": "共享带宽IP", "en": "shared bandwidth IP"},
                    "edge_label": {"zh": "提供公网带宽", "en": "provides public bandwidth"},
                    "confidence": 0.86,
                    "evidence": ["The resource associates EIPs with a shared bandwidth package."],
                    "suggested_architecture_rules_patch": {
                        "compact_attachment_edges": [
                            {
                                "resource_types": ["ALIYUN::VPC::CommonBandwidthPackageIp"],
                                "source_properties": ["BandwidthPackageId"],
                                "marker_properties": ["Eips"],
                                "source_types": ["ALIYUN::VPC::CommonBandwidthPackage"],
                                "marker_types": ["ALIYUN::VPC::EIP"],
                                "edge_style": "dotted_open",
                                "edge_label": {"zh": "提供公网带宽", "en": "provides public bandwidth"},
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::ECS::RunCommand",
                    "classification": "orchestration_action",
                    "target_resource_types": ["ALIYUN::ECS::Instance"],
                    "property_names": ["InstanceIds", "CommandContent"],
                    "label": {"zh": "云助手执行", "en": "Cloud Assistant execution"},
                    "edge_label": {"zh": "执行命令", "en": "runs command"},
                    "confidence": 0.88,
                    "evidence": ["Description says it runs a command on ECS instances."],
                    "suggested_architecture_rules_patch": {
                        "compact_orchestration_actions": [
                            {
                                "resource_types": ["ALIYUN::ECS::RunCommand"],
                                "command_properties": ["CommandContent"],
                                "target_properties": ["InstanceIds"],
                                "evidence_properties": ["CommandContent"],
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::NAS::MountTarget",
                    "classification": "bridge_attachment",
                    "target_resource_types": ["ALIYUN::NAS::FileSystem"],
                    "property_names": ["FileSystemId"],
                    "label": {"zh": "NAS挂载点", "en": "NAS mount target"},
                    "edge_label": None,
                    "confidence": 0.83,
                    "evidence": ["Mount target bridges compute access to the NAS file system."],
                    "suggested_architecture_rules_patch": {
                        "compact_bridge_attachments": [
                            {
                                "resource_types": ["ALIYUN::NAS::MountTarget"],
                                "source_properties": [],
                                "via_resource_types": ["ALIYUN::NAS::MountTarget"],
                                "via_source_properties": ["FileSystemId"],
                                "via_target_properties": ["FileSystemId"],
                                "target_types": ["ALIYUN::NAS::FileSystem"],
                                "label": {"zh": "NAS挂载点", "en": "NAS mount target"},
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::ESS::ScalingConfiguration",
                    "classification": "concept_node",
                    "target_resource_types": [
                        "ALIYUN::ESS::ScalingGroup",
                        "ALIYUN::ECS::Instance",
                    ],
                    "property_names": ["ScalingGroupId", "InstanceId"],
                    "label": {"zh": "伸缩 ECS 实例", "en": "Scaled ECS instances"},
                    "edge_label": {"zh": "伸缩配置", "en": "scaling config"},
                    "confidence": 0.9,
                    "evidence": ["Scaling configuration links a scaling group and source ECS instance."],
                    "suggested_architecture_rules_patch": {
                        "compact_concept_nodes": [
                            {
                                "via_resource_types": ["ALIYUN::ESS::ScalingConfiguration"],
                                "controller_property": "ScalingGroupId",
                                "source_property": "InstanceId",
                                "id_suffix": "ScaledEcs",
                                "resource_type": "CONCEPT::ESS::ScaledECS",
                                "label": {"zh": "伸缩 ECS 实例", "en": "Scaled ECS instances"},
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::ECS::VPC",
                    "classification": "container",
                    "target_resource_types": [],
                    "property_names": [],
                    "label": {"zh": "专有网络 VPC", "en": "VPC"},
                    "edge_label": None,
                    "confidence": 0.94,
                    "evidence": ["VPC is a network boundary."],
                    "suggested_architecture_rules_patch": {
                        "network_layer_types": ["ALIYUN::ECS::VPC"],
                        "containment_layer_types": {"vpc": ["ALIYUN::ECS::VPC"]},
                    },
                },
                {
                    "resource_type": "ALIYUN::ECS::SecurityGroup",
                    "classification": "attachment",
                    "target_resource_types": ["ALIYUN::ECS::Instance"],
                    "property_names": ["NotAProperty"],
                    "label": {"zh": "安全组", "en": "security group"},
                    "edge_label": None,
                    "confidence": 0.9,
                    "evidence": ["Invalid property should be rejected."],
                    "suggested_architecture_rules_patch": {
                        "compact_child_attachments": [
                            {
                                "resource_types": ["ALIYUN::ECS::SecurityGroup"],
                                "target_properties": ["NotAProperty"],
                                "target_types": ["ALIYUN::ECS::Instance"],
                                "label": {"zh": "安全组", "en": "security group"},
                            }
                        ]
                    },
                },
                {
                    "resource_type": "ALIYUN::ECS::Instance",
                    "classification": "attachment_edge",
                    "target_resource_types": ["ALIYUN::ECS::Instance"],
                    "property_names": ["VSwitchId"],
                    "label": {"zh": "错误实线", "en": "bad solid"},
                    "edge_label": {"zh": "业务访问", "en": "traffic"},
                    "confidence": 0.92,
                    "evidence": ["Attachment edges must not become solid traffic arrows."],
                    "suggested_architecture_rules_patch": {
                        "compact_attachment_edges": [
                            {
                                "resource_types": ["ALIYUN::ECS::Instance"],
                                "source_properties": ["VSwitchId"],
                                "marker_properties": ["VSwitchId"],
                                "source_types": ["ALIYUN::ECS::VSwitch"],
                                "marker_types": ["ALIYUN::ECS::Instance"],
                                "edge_style": "solid_arrow",
                                "edge_label": {"zh": "业务访问", "en": "traffic"},
                            }
                        ]
                    },
                },
            ]
        },
        ensure_ascii=False,
    )


def test_parse_validate_and_review_draft_rules_before_applying_patches() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(_draft_payload())

    validated = validate_draft_rules(drafts, facts)
    reviewed = review_valid_draft_rules(validated)

    accepted = [decision for decision in reviewed if decision.accepted]
    rejected = [decision for decision in reviewed if not decision.accepted]

    assert len(drafts) == 8
    assert {decision.draft.resource_type for decision in accepted} == {
        "ALIYUN::VPC::SnatEntry",
        "ALIYUN::VPC::CommonBandwidthPackageIp",
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::NAS::MountTarget",
        "ALIYUN::ESS::ScalingConfiguration",
        "ALIYUN::ECS::VPC",
    }
    assert any("unknown property" in " ".join(decision.reasons) for decision in rejected)
    assert any("solid" in " ".join(decision.reasons) for decision in rejected)

    raw_rules: dict = {
        "network_layer_types": [],
        "containment_layer_types": {},
        "compact_child_attachments": [],
        "compact_bridge_attachments": [],
        "compact_attachment_edges": [],
        "compact_orchestration_actions": [],
        "compact_concept_nodes": [],
    }
    updated = apply_reviewed_draft_patches(raw_rules, reviewed)

    assert updated["network_layer_types"] == ["ALIYUN::ECS::VPC"]
    assert updated["containment_layer_types"] == {"vpc": ["ALIYUN::ECS::VPC"]}
    assert updated["compact_child_attachments"][0]["resource_types"] == ["ALIYUN::VPC::SnatEntry"]
    assert updated["compact_attachment_edges"][0]["edge_style"] == "dotted_open"
    assert updated["compact_orchestration_actions"][0]["resource_types"] == ["ALIYUN::ECS::RunCommand"]
    assert updated["compact_concept_nodes"][0]["resource_type"] == "CONCEPT::ESS::ScaledECS"
    assert all(
        item.get("resource_types") != ["ALIYUN::ECS::SecurityGroup"] for item in updated["compact_child_attachments"]
    )

    updated_again = apply_reviewed_draft_patches(updated, reviewed)
    assert updated_again["compact_child_attachments"] == updated["compact_child_attachments"]
    assert updated_again["compact_orchestration_actions"] == updated["compact_orchestration_actions"]

    report = render_draft_review_report_markdown(reviewed)
    assert "| Draft rules | 8 |" in report
    assert "| Accepted | 6 |" in report
    assert "| Rejected | 2 |" in report
    assert "## By Classification" in report


def test_rejects_draft_with_unknown_target_or_patch_key() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::VPC::SnatEntry",
                        "classification": "attachment",
                        "target_resource_types": ["ALIYUN::NOT::Real"],
                        "property_names": ["SnatTableId"],
                        "label": "SNAT entry",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["bad target"],
                        "suggested_architecture_rules_patch": {"compact_child_attachments": []},
                    },
                    {
                        "resource_type": "ALIYUN::VPC::SnatEntry",
                        "classification": "attachment",
                        "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                        "property_names": ["SnatTableId"],
                        "label": "SNAT entry",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["bad patch key"],
                        "suggested_architecture_rules_patch": {"made_up_key": []},
                    },
                ]
            }
        )
    )

    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))

    assert [decision.accepted for decision in reviewed] == [False, False]
    assert "unknown target resource type" in " ".join(reviewed[0].reasons)
    assert "unsupported patch key" in " ".join(reviewed[1].reasons)


def test_rejects_nested_patch_with_unknown_resource_type_or_property() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::VPC::SnatEntry",
                        "classification": "attachment",
                        "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                        "property_names": ["SnatTableId"],
                        "label": "SNAT entry",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["bad nested patch"],
                        "suggested_architecture_rules_patch": {
                            "compact_child_attachments": [
                                {
                                    "resource_types": ["ALIYUN::NOT::Real"],
                                    "target_properties": ["NotAProperty"],
                                    "target_types": ["ALIYUN::VPC::NatGateway"],
                                    "label": "SNAT entry",
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )

    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))

    assert reviewed[0].accepted is False
    reasons = " ".join(reviewed[0].reasons)
    assert "unknown patch resource type" in reasons
    assert "unknown patch property" in reasons


def test_attachment_edge_draft_can_use_edge_label_without_node_label() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::VPC::CommonBandwidthPackageIp",
                        "classification": "attachment_edge",
                        "target_resource_types": [
                            "ALIYUN::VPC::CommonBandwidthPackage",
                            "ALIYUN::VPC::EIP",
                        ],
                        "property_names": ["BandwidthPackageId", "Eips"],
                        "label": None,
                        "edge_label": {"zh": "提供公网带宽", "en": "provides bandwidth"},
                        "confidence": 0.9,
                        "evidence": ["The resource associates EIPs with a shared bandwidth package."],
                        "suggested_architecture_rules_patch": {
                            "compact_attachment_edges": [
                                {
                                    "resource_types": ["ALIYUN::VPC::CommonBandwidthPackageIp"],
                                    "source_properties": ["BandwidthPackageId"],
                                    "marker_properties": ["Eips"],
                                    "source_types": ["ALIYUN::VPC::CommonBandwidthPackage"],
                                    "marker_types": ["ALIYUN::VPC::EIP"],
                                    "edge_style": "dotted_open",
                                    "edge_label": {"zh": "提供公网带宽", "en": "provides bandwidth"},
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )

    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))

    assert reviewed[0].accepted is True


def test_bridge_attachment_requires_via_resource_types() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::NAS::MountTarget",
                        "classification": "bridge_attachment",
                        "target_resource_types": ["ALIYUN::NAS::FileSystem"],
                        "property_names": ["FileSystemId"],
                        "label": "NAS mount target",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["Association resource without a via resource cannot fold in renderer."],
                        "suggested_architecture_rules_patch": {
                            "compact_bridge_attachments": [
                                {
                                    "resource_types": ["ALIYUN::NAS::MountTarget"],
                                    "source_properties": ["FileSystemId"],
                                    "via_resource_types": [],
                                    "via_source_properties": [],
                                    "via_target_properties": [],
                                    "target_types": ["ALIYUN::NAS::FileSystem"],
                                    "label": "NAS mount target",
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )

    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))

    assert reviewed[0].accepted is False
    assert "via_resource_types" in " ".join(reviewed[0].reasons)


def test_apply_localizes_string_patch_labels_from_resource_facts() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::VPC::SnatEntry",
                        "classification": "attachment",
                        "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                        "property_names": ["SnatTableId"],
                        "label": "SNAT entry",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["SNAT entry configures NAT gateway."],
                        "suggested_architecture_rules_patch": {
                            "compact_child_attachments": [
                                {
                                    "resource_types": ["ALIYUN::VPC::SnatEntry"],
                                    "target_properties": ["SnatTableId"],
                                    "target_types": ["ALIYUN::VPC::NatGateway"],
                                    "label": "SNAT entry",
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )
    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))

    updated = apply_reviewed_draft_patches({"compact_child_attachments": []}, reviewed, facts=facts)

    assert updated["compact_child_attachments"][0]["label"] == {
        "zh": "SNAT 条目",
        "en": "SNAT entry",
    }


def test_apply_skips_semantic_duplicate_resource_type_rules() -> None:
    facts = _facts()
    drafts = parse_draft_rules_response(
        json.dumps(
            {
                "draft_rules": [
                    {
                        "resource_type": "ALIYUN::VPC::SnatEntry",
                        "classification": "attachment",
                        "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                        "property_names": ["SnatTableId"],
                        "label": "SNAT config",
                        "edge_label": None,
                        "confidence": 0.9,
                        "evidence": ["same semantic resource type with a different label"],
                        "suggested_architecture_rules_patch": {
                            "compact_child_attachments": [
                                {
                                    "resource_types": ["ALIYUN::VPC::SnatEntry"],
                                    "target_properties": ["SnatTableId"],
                                    "target_types": ["ALIYUN::VPC::NatGateway"],
                                    "label": "SNAT config",
                                }
                            ]
                        },
                    }
                ]
            }
        )
    )
    reviewed = review_valid_draft_rules(validate_draft_rules(drafts, facts))
    raw_rules = {
        "compact_child_attachments": [
            {
                "resource_types": ["ALIYUN::VPC::SnatEntry"],
                "target_properties": ["SnatTableId"],
                "target_types": ["ALIYUN::VPC::NatGateway"],
                "label": "SNAT entry",
            }
        ]
    }

    updated = apply_reviewed_draft_patches(raw_rules, reviewed)

    assert updated["compact_child_attachments"] == raw_rules["compact_child_attachments"]


def test_parse_accepts_top_level_list_from_llm() -> None:
    drafts = parse_draft_rules_response(
        json.dumps(
            [
                {
                    "resource_type": "ALIYUN::VPC::SnatEntry",
                    "classification": "attachment",
                    "target_resource_types": ["ALIYUN::VPC::NatGateway"],
                    "property_names": ["SnatTableId"],
                    "label": "SNAT entry",
                    "edge_label": None,
                    "confidence": 0.9,
                    "evidence": ["LLM returned a top-level list."],
                    "suggested_architecture_rules_patch": {"compact_child_attachments": []},
                }
            ]
        )
    )

    assert len(drafts) == 1
    assert drafts[0].resource_type == "ALIYUN::VPC::SnatEntry"
