from __future__ import annotations

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    RosResourceTypeDetail,
    build_resource_inventory_snapshot,
)
from iac_code.pipeline.engine.architecture_rule_candidates import (
    build_resource_type_decisions,
    extract_rule_candidates,
    render_rule_candidate_report_markdown,
)


def _repo() -> ArchitectureMetaRepository:
    def prop(name: str, target: str) -> dict:
        return {"ROS": name, "Type": "String", "RelatedTo": [{"ResourceType": f"ROS/{target}"}]}

    return ArchitectureMetaRepository.from_raw(
        categories=[
            {"CategoryCode": "network", "ProductCodes": ["ecs", "vpc"]},
            {"CategoryCode": "storage", "ProductCodes": ["nas"]},
            {"CategoryCode": "compute", "ProductCodes": ["ess"]},
        ],
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
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::Instance"},
                "ProductCode": "ecs",
                "Name": {"en": "ECS Instance", "zh": "ECS 实例"},
                "Properties": [prop("VSwitchId", "ALIYUN::ECS::VSwitch")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::EIP"},
                "ProductCode": "vpc",
                "Name": {"en": "EIP", "zh": "弹性公网 IP"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::EIPAssociation"},
                "ProductCode": "vpc",
                "Name": {"en": "Associate EIP", "zh": "弹性公网 IP 绑定"},
                "MainResourceType": {
                    "ResourceType": "ROS/ALIYUN::VPC::EIP",
                    "RefProperty": "ROS/AllocationId",
                },
                "Properties": [
                    prop("AllocationId", "ALIYUN::VPC::EIP"),
                    prop("InstanceId", "ALIYUN::ECS::Instance"),
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
                "ResourceType": {"ROS": "ROS/ALIYUN::VPC::CommonBandwidthPackageIp"},
                "ProductCode": "vpc",
                "Name": {"en": "Shared Bandwidth IP", "zh": "共享带宽 IP"},
                "Properties": [
                    prop("BandwidthPackageId", "ALIYUN::VPC::CommonBandwidthPackage"),
                    {"ROS": "Eips", "Type": "Json"},
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
                "Properties": [
                    prop("FileSystemId", "ALIYUN::NAS::FileSystem"),
                    prop("VSwitchId", "ALIYUN::ECS::VSwitch"),
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::NAS::AccessGroup"},
                "ProductCode": "nas",
                "Name": {"en": "NAS Permission Group", "zh": "NAS 权限组"},
                "Properties": [],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::NAS::AccessRule"},
                "ProductCode": "nas",
                "Name": {"en": "NAS Permission Rule", "zh": "NAS 权限规则"},
                "Properties": [prop("AccessGroupName", "ALIYUN::NAS::AccessGroup")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ECS::RunCommand"},
                "ProductCode": "ecs",
                "Name": {"en": "Run Cloud Assistant Command", "zh": "执行云助手命令"},
                "Properties": [prop("InstanceIds", "ALIYUN::ECS::Instance")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ESS::ScalingGroup"},
                "ProductCode": "ess",
                "Name": {"en": "Scaling Group", "zh": "伸缩组"},
                "Properties": [prop("VSwitchId", "ALIYUN::ECS::VSwitch")],
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
                "ResourceType": {"ROS": "ROS/ALIYUN::ROS::WaitCondition"},
                "ProductCode": "ros",
                "Name": {"en": "Wait Condition", "zh": "等待条件"},
                "Properties": [],
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


def _snapshot():
    resource_types = [
        "ALIYUN::ECS::VPC",
        "ALIYUN::ECS::VSwitch",
        "ALIYUN::ECS::Instance",
        "ALIYUN::VPC::EIP",
        "ALIYUN::VPC::EIPAssociation",
        "ALIYUN::VPC::NatGateway",
        "ALIYUN::VPC::SnatEntry",
        "ALIYUN::VPC::CommonBandwidthPackage",
        "ALIYUN::VPC::CommonBandwidthPackageIp",
        "ALIYUN::NAS::FileSystem",
        "ALIYUN::NAS::MountTarget",
        "ALIYUN::NAS::AccessGroup",
        "ALIYUN::NAS::AccessRule",
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::ESS::ScalingGroup",
        "ALIYUN::ESS::ScalingConfiguration",
        "ALIYUN::ROS::WaitCondition",
        "ALIYUN::NEW::ApiOnlyPolicy",
        "ALIYUN::VPC::EIPPro",
        "ALIYUN::CEN::RouteEntry",
        "ALIYUN::CS::ClusterAddons",
        "ALIYUN::POLARDB::DBClusterSecurityIP",
    ]
    return build_resource_inventory_snapshot(
        api_resource_types=resource_types,
        details_by_type={
            "ALIYUN::VPC::SnatEntry": _detail(
                "ALIYUN::VPC::SnatEntry",
                "An SNAT entry allows resources in a VPC or vSwitch to access the Internet through a NAT gateway.",
                {
                    "SnatTableId": {"Description": "The ID of the SNAT table."},
                    "SnatIp": {"Description": "The public IP address."},
                },
            ),
            "ALIYUN::NAS::MountTarget": _detail(
                "ALIYUN::NAS::MountTarget",
                "A mount target is used by compute resources to mount a NAS file system.",
                {
                    "AccessGroupName": {"Description": "The name of the permission group."},
                    "FileSystemId": {"Description": "The ID of the file system."},
                },
            ),
            "ALIYUN::VPC::CommonBandwidthPackageIp": _detail(
                "ALIYUN::VPC::CommonBandwidthPackageIp",
                "Associates EIPs with an Internet shared bandwidth instance.",
                {
                    "BandwidthPackageId": {"Description": "The ID of the Internet Shared Bandwidth instance."},
                    "Eips": {"Description": "List of EIPs associated with the shared bandwidth instance."},
                },
            ),
            "ALIYUN::ECS::RunCommand": _detail(
                "ALIYUN::ECS::RunCommand",
                "Runs a Cloud Assistant command on one or more ECS instances.",
                {
                    "CommandContent": {"Description": "The command content."},
                    "InstanceIds": {"Description": "The ECS instances on which to run the command."},
                },
            ),
            "ALIYUN::ESS::ScalingConfiguration": _detail(
                "ALIYUN::ESS::ScalingConfiguration",
                "A scaling configuration defines ECS instance settings used by a scaling group.",
                {
                    "ScalingGroupId": {"Description": "The ID of the scaling group."},
                    "InstanceId": {"Description": "The source ECS instance used to create the scaling configuration."},
                },
            ),
            "ALIYUN::NEW::ApiOnlyPolicy": _detail(
                "ALIYUN::NEW::ApiOnlyPolicy",
                "A policy resource returned by ListResourceTypes but missing from local meta.",
                {"PolicyName": {"Description": "The policy name."}},
            ),
            "ALIYUN::VPC::EIPPro": _detail(
                "ALIYUN::VPC::EIPPro",
                "A pro elastic public IP address that can be associated with resources in a VPC.",
                {
                    "DeletionProtection": {"Description": "Specifies whether to enable deletion protection."},
                },
            ),
            "ALIYUN::CEN::RouteEntry": _detail(
                "ALIYUN::CEN::RouteEntry",
                "A route entry that forwards traffic to a VPC or transit router.",
            ),
            "ALIYUN::CS::ClusterAddons": _detail(
                "ALIYUN::CS::ClusterAddons",
                "Configures addons for a Container Service Kubernetes cluster.",
                {
                    "InstallCloudMonitor": {"Description": "Specifies whether to enable the CloudMonitor addon."},
                },
            ),
            "ALIYUN::POLARDB::DBClusterSecurityIP": _detail(
                "ALIYUN::POLARDB::DBClusterSecurityIP",
                "Configures the IP whitelist of a PolarDB cluster.",
            ),
        },
        meta_repository=_repo(),
        fetched_at="2026-06-26T00:00:00Z",
    )


def test_extracts_all_eight_rule_candidate_categories() -> None:
    candidates = extract_rule_candidates(_snapshot())

    by_category = {candidate.category for candidate in candidates}

    assert {
        "container",
        "supplemental_relation",
        "display",
        "attachment",
        "bridge_attachment",
        "attachment_edge",
        "orchestration_action",
        "concept_node",
    }.issubset(by_category)

    attachment = next(
        candidate
        for candidate in candidates
        if candidate.category == "attachment" and candidate.resource_type == "ALIYUN::VPC::EIPAssociation"
    )
    assert attachment.suggested_config["compact_attachment_marker_types"] == ["ALIYUN::VPC::EIP"]
    assert "MainResourceType" in " ".join(attachment.evidence)

    bridge = next(
        candidate
        for candidate in candidates
        if candidate.category == "bridge_attachment" and candidate.resource_type == "ALIYUN::NAS::AccessRule"
    )
    assert bridge.target_resource_types == ("ALIYUN::NAS::FileSystem",)
    assert "ALIYUN::NAS::MountTarget" in bridge.suggested_config["via_resource_types"]

    attachment_edge = next(
        candidate
        for candidate in candidates
        if candidate.category == "attachment_edge"
        and candidate.resource_type == "ALIYUN::VPC::CommonBandwidthPackageIp"
    )
    assert attachment_edge.target_resource_types == (
        "ALIYUN::VPC::CommonBandwidthPackage",
        "ALIYUN::VPC::EIP",
    )
    assert attachment_edge.suggested_config["edge_style"] == "dotted_open"


def test_container_candidates_do_not_match_product_or_description_noise() -> None:
    candidates = extract_rule_candidates(_snapshot())
    container_types = {candidate.resource_type for candidate in candidates if candidate.category == "container"}

    assert "ALIYUN::ECS::VPC" in container_types
    assert "ALIYUN::ECS::VSwitch" in container_types
    assert "ALIYUN::VPC::EIPPro" not in container_types
    assert "ALIYUN::CEN::RouteEntry" not in container_types
    assert "ALIYUN::CS::ClusterAddons" not in container_types
    assert "ALIYUN::POLARDB::DBClusterSecurityIP" not in container_types


def test_every_api_resource_type_gets_a_decision_with_evidence() -> None:
    snapshot = _snapshot()
    candidates = extract_rule_candidates(snapshot)

    decisions = build_resource_type_decisions(snapshot, candidates)

    assert set(decisions) == set(snapshot.api_resource_types)
    assert decisions["ALIYUN::ECS::VPC"].decision == "container"
    assert decisions["ALIYUN::VPC::EIPAssociation"].decision == "attachment"
    assert decisions["ALIYUN::ECS::RunCommand"].decision == "orchestration_action"
    assert decisions["ALIYUN::ESS::ScalingConfiguration"].decision == "concept_node"
    assert decisions["ALIYUN::NEW::ApiOnlyPolicy"].decision == "needs_review"
    assert decisions["ALIYUN::VPC::EIPPro"].decision == "needs_review"
    assert decisions["ALIYUN::NEW::ApiOnlyPolicy"].evidence


def test_renders_markdown_report_grouped_by_category_and_product() -> None:
    snapshot = _snapshot()
    candidates = extract_rule_candidates(snapshot)
    decisions = build_resource_type_decisions(snapshot, candidates)

    markdown = render_rule_candidate_report_markdown(snapshot, candidates, decisions)

    assert "# ROS 架构图规则候选报告" in markdown
    assert "| API resource types | 22 |" in markdown
    assert "## Candidate Categories" in markdown
    assert "### attachment" in markdown
    assert "`ALIYUN::VPC::EIPAssociation`" in markdown
    assert "## Decisions by Product" in markdown
    assert "### `vpc`" in markdown
    assert "`ALIYUN::VPC::CommonBandwidthPackageIp` | `attachment_edge`" in markdown
