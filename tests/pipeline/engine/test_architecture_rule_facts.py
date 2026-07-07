from __future__ import annotations

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    RosResourceTypeDetail,
    build_resource_inventory_snapshot,
)
from iac_code.pipeline.engine.architecture_rule_facts import build_resource_rule_facts
from iac_code.pipeline.engine.architecture_rules import ArchitectureRules


def _repo() -> ArchitectureMetaRepository:
    def prop(name: str, target: str) -> dict:
        return {"ROS": name, "Type": "String", "RelatedTo": [{"ResourceType": f"ROS/{target}"}]}

    return ArchitectureMetaRepository.from_raw(
        categories=[
            {"CategoryCode": "network", "ProductCodes": ["ecs", "vpc"]},
            {"CategoryCode": "database", "ProductCodes": ["polardb"]},
            {"CategoryCode": "storage", "ProductCodes": ["nas"]},
        ],
        products=[
            {"ProductCode": "ecs", "Name": {"en": "ECS", "zh": "云服务器"}, "RelevantCodes": {"ROS": "ECS"}},
            {"ProductCode": "vpc", "Name": {"en": "VPC", "zh": "专有网络"}, "RelevantCodes": {"ROS": "VPC"}},
            {"ProductCode": "nas", "Name": {"en": "NAS", "zh": "文件存储"}, "RelevantCodes": {"ROS": "NAS"}},
            {
                "ProductCode": "polardb",
                "Name": {"en": "PolarDB", "zh": "云原生数据库"},
                "RelevantCodes": {"ROS": "POLARDB"},
            },
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
                    prop("AccessGroupName", "ALIYUN::NAS::AccessGroup"),
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
                "ProductCode": "ecs",
                "Name": {"en": "Scaling Group", "zh": "伸缩组"},
                "Properties": [prop("VSwitchId", "ALIYUN::ECS::VSwitch")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::ESS::ScalingConfiguration"},
                "ProductCode": "ecs",
                "Name": {"en": "Scaling Configuration", "zh": "伸缩配置"},
                "Properties": [
                    prop("ScalingGroupId", "ALIYUN::ESS::ScalingGroup"),
                    prop("InstanceId", "ALIYUN::ECS::Instance"),
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::POLARDB::DBClusterSecurityIP"},
                "ProductCode": "polardb",
                "Name": {"en": "PolarDB whitelist", "zh": "PolarDB 白名单"},
                "Properties": [prop("DBClusterId", "ALIYUN::POLARDB::DBCluster")],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::POLARDB::DBCluster"},
                "ProductCode": "polardb",
                "Name": {"en": "PolarDB Cluster", "zh": "PolarDB 集群"},
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
        "ALIYUN::NAS::FileSystem",
        "ALIYUN::NAS::MountTarget",
        "ALIYUN::NAS::AccessGroup",
        "ALIYUN::NAS::AccessRule",
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::ESS::ScalingGroup",
        "ALIYUN::ESS::ScalingConfiguration",
        "ALIYUN::POLARDB::DBCluster",
        "ALIYUN::POLARDB::DBClusterSecurityIP",
    ]
    return build_resource_inventory_snapshot(
        api_resource_types=resource_types,
        details_by_type={
            "ALIYUN::VPC::EIPAssociation": _detail(
                "ALIYUN::VPC::EIPAssociation",
                "Associates an elastic IP address with an ECS instance or SLB instance.",
                {
                    "AllocationId": {"Type": "string", "Required": True, "Description": "The ID of the EIP."},
                    "InstanceId": {
                        "Type": "string",
                        "Required": True,
                        "Description": "The ID of the instance to associate.",
                    },
                },
            ),
            "ALIYUN::NAS::AccessRule": _detail(
                "ALIYUN::NAS::AccessRule",
                "Configures an access rule for a NAS permission group.",
                {"AccessGroupName": {"Description": "The permission group name."}},
            ),
            "ALIYUN::ECS::RunCommand": _detail(
                "ALIYUN::ECS::RunCommand",
                "Runs a Cloud Assistant command on ECS instances.",
                {
                    "CommandContent": {"Description": "The command content."},
                    "InstanceIds": {"Description": "The ECS instances on which to run the command."},
                },
            ),
            "ALIYUN::ESS::ScalingConfiguration": _detail(
                "ALIYUN::ESS::ScalingConfiguration",
                "Defines ECS settings used by a scaling group.",
                {
                    "ScalingGroupId": {"Description": "The scaling group ID."},
                    "InstanceId": {"Description": "The source ECS instance."},
                },
            ),
            "ALIYUN::POLARDB::DBClusterSecurityIP": _detail(
                "ALIYUN::POLARDB::DBClusterSecurityIP",
                "Configures a whitelist for a PolarDB cluster.",
            ),
        },
        meta_repository=_repo(),
        fetched_at="2026-06-27T00:00:00Z",
    )


def test_builds_resource_facts_and_signals_without_final_decisions() -> None:
    bundle = build_resource_rule_facts(_snapshot(), ArchitectureRules.load_default())

    payload = bundle.to_dict()

    assert set(payload) == {"summary", "resource_facts", "rule_signals"}
    assert "candidates" not in payload
    assert "decisions" not in payload
    assert payload["summary"]["api_resource_types"] == 14
    assert payload["summary"]["resource_facts"] == 14
    assert payload["summary"]["rule_signals"] >= 6

    eip_fact = next(
        fact for fact in payload["resource_facts"] if fact["resource_type"] == "ALIYUN::VPC::EIPAssociation"
    )
    assert eip_fact["main_resource_type"] == {
        "resource_type": "ALIYUN::VPC::EIP",
        "ref_property": "AllocationId",
    }
    assert eip_fact["related_properties"]["AllocationId"] == ["ALIYUN::VPC::EIP"]
    assert eip_fact["properties"]["InstanceId"]["description"] == "The ID of the instance to associate."
    assert "fixed_rule_hits" in eip_fact

    signal_categories = {signal["category"] for signal in payload["rule_signals"]}
    assert {
        "attachment",
        "bridge_attachment",
        "orchestration_action",
        "concept_node",
        "container",
    }.issubset(signal_categories)
    assert all("decision" not in signal for signal in payload["rule_signals"])


def test_container_signal_does_not_classify_whitelist_noise_as_container() -> None:
    bundle = build_resource_rule_facts(_snapshot(), ArchitectureRules.load_default())

    container_types = {signal.resource_type for signal in bundle.rule_signals if signal.category == "container"}

    assert "ALIYUN::ECS::VPC" in container_types
    assert "ALIYUN::ECS::VSwitch" in container_types
    assert "ALIYUN::POLARDB::DBClusterSecurityIP" not in container_types
