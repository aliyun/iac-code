"""Authoritative server-side selector profiles.

The browser bundle is a renderer, never the source of truth. This module owns
stable selector ids, the semantic whitelist, metadata contracts, fixed query
operation keys, and scalar output contracts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

SelectionKind = Literal["cloud_resource", "cloud_resource_derived_value"]
OutputKind = Literal["resource_id", "resource_name", "endpoint", "version", "secret_reference", "derived_value"]
InteractionKind = Literal["single", "hierarchical", "derived"]

_VALUE_PATTERNS_BY_OUTPUT_KIND: dict[OutputKind, str] = {
    "resource_id": r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$",
    "resource_name": r"^[^\x00-\x1f\x7f<>{}]+$",
    "endpoint": r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?[A-Za-z0-9][A-Za-z0-9._:/?=&%#@+-]*$",
    "version": r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]*$",
    "secret_reference": r"^[^\x00-\x1f\x7f]+$",
    "derived_value": r"^[^\x00-\x1f\x7f<>{}]+$",
}


def string_schema(
    *, description: str = "", default_source: str | None = None, affects: str = "query_filter", **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "string", "maxLength": 1024, "affects": [affects]}
    if description:
        result["description"] = description
    if default_source:
        result["default_source"] = default_source
    result.update(extra)
    return result


def boolean_schema(*, default: bool = False, affects: str = "ui_behavior") -> dict[str, Any]:
    return {"type": "boolean", "default": default, "affects": [affects]}


REGION = string_schema(
    description="资源所在地域",
    default_source="session.default_region_id",
    affects="query_scope",
    maxLength=64,
    pattern=r"^[A-Za-z0-9-]+$",
)


def metadata_schema(
    properties: dict[str, dict[str, Any]] | None = None, *, required: tuple[str, ...] = ()
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "maxProperties": 32,
        "properties": properties or {},
    }
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True)
class QueryOperation:
    key: str
    product: str
    action: str
    dynamic_parameters: tuple[str, ...] = ()
    metadata_parameters: tuple[tuple[str, str], ...] = ()
    fixed_parameters: tuple[tuple[str, Any], ...] = ()
    request_kind: str = "dataApi"
    batch_parameters: tuple[str, ...] = ()
    api_version: str | None = None
    style: str | None = None
    method: str | None = None
    pathname_template: str | None = None
    path_parameters: tuple[str, ...] = ()
    parameters_in_body: bool = False
    # Key into the separately generated, fail-closed response projection
    # registry.  Keeping this explicit prevents a newly added operation from
    # falling back to an unrestricted recursive response pass-through.
    response_projector: str | None = None
    # Complete browser wire-shape accepted for this operation.  This is
    # intentionally separate from dynamic_parameters: metadata, source and
    # fixed values may be echoed by ORE but are never browser-owned.
    accepted_parameters: tuple[str, ...] = ()
    batch_dynamic_parameters: tuple[str, ...] = ()


# Fixed server-side reads that enforce selector constraints but are not emitted
# by ORE itself.  Keep them outside the browser capability surface while still
# including them in request-contract and opt-in live coverage.
SERVER_AUXILIARY_OPERATIONS = (
    QueryOperation(
        key="ecs.instance.cloud_assistant_status",
        product="ecs",
        action="DescribeCloudAssistantStatus",
        dynamic_parameters=("InstanceId",),
        api_version="2014-05-26",
    ),
)


@dataclass(frozen=True)
class SelectorProfile:
    selector_id: str
    association_property: str
    title: str
    description: str
    selection_kind: SelectionKind
    output_kind: OutputKind
    resource_type: str | None
    metadata_schema: dict[str, Any] = field(default_factory=metadata_schema)
    source_selector_id: str | None = None
    # AssociationPropertyMetadata key populated from ``source.value``.
    source_parameter_key: str | None = None
    # Per-operation wire binding for the same source.  ``None`` means the ORE
    # component filters the returned envelope client-side instead of sending
    # the source to that API.  Missing operations fall back to
    # source_parameter_key.
    source_operation_parameters: tuple[tuple[str, str | None], ...] = ()
    operations: tuple[QueryOperation, ...] = ()
    value_pattern: str | None = None
    value_pattern_by_attribute: dict[str, str] | None = None
    value_max_length: int = 1024
    enabled: bool = False
    unsupported_reason: str | None = None
    ore_association_property: str | None = None
    output_kind_by_attribute: dict[str, OutputKind] | None = None
    value_fields: tuple[str, ...] = ()
    # Model-facing discovery hints. They are intentionally excluded from the
    # browser capability hash because they do not change rendering or query contracts.
    search_terms: tuple[str, ...] = ()
    interaction_kind: InteractionKind = "single"
    interaction_steps: tuple[str, ...] = ()
    usage_hint: str | None = None

    @property
    def standalone_association_property(self) -> str:
        return self.ore_association_property or self.association_property

    def source_parameter_for(self, operation_key: str) -> str | None:
        for key, parameter in self.source_operation_parameters:
            if key == operation_key:
                return parameter
        return self.source_parameter_key


def enabled_profile(
    selector_id: str,
    association_property: str,
    title: str,
    *,
    output_kind: OutputKind,
    resource_type: str | None,
    metadata: dict[str, Any],
    operations: tuple[QueryOperation, ...],
    source_selector_id: str | None = None,
    source_parameter_key: str | None = None,
    source_operation_parameters: tuple[tuple[str, str | None], ...] = (),
    value_pattern: str | None = None,
    selection_kind: SelectionKind = "cloud_resource",
    description: str | None = None,
    output_kind_by_attribute: dict[str, OutputKind] | None = None,
    value_pattern_by_attribute: dict[str, str] | None = None,
    search_terms: tuple[str, ...] = (),
    interaction_kind: InteractionKind | None = None,
    interaction_steps: tuple[str, ...] = (),
    usage_hint: str | None = None,
) -> SelectorProfile:
    return SelectorProfile(
        selector_id=selector_id,
        association_property=association_property,
        title=title,
        description=description or "选择一个当前账号可见的已有{}".format(title),
        selection_kind=selection_kind,
        output_kind=output_kind,
        resource_type=resource_type,
        metadata_schema=metadata,
        source_selector_id=source_selector_id,
        source_parameter_key=source_parameter_key,
        source_operation_parameters=source_operation_parameters,
        operations=tuple(
            replace(
                operation,
                accepted_parameters=operation.accepted_parameters
                or tuple(
                    dict.fromkeys(
                        (
                            *operation.dynamic_parameters,
                            *(api_parameter for _, api_parameter in operation.metadata_parameters),
                            *(api_parameter for api_parameter, _ in operation.fixed_parameters),
                        )
                    )
                ),
                response_projector=operation.response_projector or operation.key,
            )
            for operation in operations
        ),
        value_pattern=value_pattern,
        enabled=True,
        output_kind_by_attribute=output_kind_by_attribute,
        value_pattern_by_attribute=value_pattern_by_attribute,
        search_terms=search_terms,
        interaction_kind=interaction_kind or ("derived" if source_selector_id else "single"),
        interaction_steps=interaction_steps,
        usage_hint=usage_hint,
    )


ENABLED_PROFILES = (
    enabled_profile(
        "ecs.instance",
        "ALIYUN::ECS::Instance::InstanceId",
        "ECS 实例",
        output_kind="resource_id",
        resource_type="ALIYUN::ECS::Instance",
        value_pattern=r"^i-[A-Za-z0-9][A-Za-z0-9._-]*$",
        metadata=metadata_schema(
            {
                "RegionId": REGION,
                "InstanceType": string_schema(),
                "InstanceTypeFamily": string_schema(),
                "Platform": string_schema(),
                "OSType": string_schema(),
                "Status": string_schema(),
                "DisabledStatus": string_schema(),
                "NetworkType": string_schema(),
                "DisabledNetworkType": boolean_schema(),
                "ShowNetworkType": boolean_schema(),
                "InternetChargeType": string_schema(),
                "DisabledInternetChargeType": boolean_schema(),
                "ShowInternetChargeType": boolean_schema(),
                "ChargeType": string_schema(),
                "DisabledChargeType": boolean_schema(),
                "ShowChargeType": boolean_schema(),
                "OnlyCloudAssistantExecutable": boolean_schema(affects="query_filter"),
                "PublicIpRequired": boolean_schema(affects="query_filter"),
                "OnlyShowSelector": boolean_schema(default=True),
                "PackageName": string_schema(),
                "DisabledOSType": boolean_schema(),
                "ShowOSType": boolean_schema(default=True),
            }
        ),
        operations=(
            QueryOperation(
                "ecs.instance.list",
                "ecs",
                "DescribeInstances",
                ("MaxResults", "NextToken", "InstanceName"),
                (
                    ("InstanceType", "InstanceType"),
                    ("InstanceTypeFamily", "InstanceTypeFamily"),
                    ("Status", "Status"),
                    ("NetworkType", "InstanceNetworkType"),
                    ("InternetChargeType", "InternetChargeType"),
                    ("ChargeType", "InstanceChargeType"),
                ),
                api_version="2014-05-26",
            ),
        ),
    ),
    enabled_profile(
        "vpc.vswitch",
        "ALIYUN::VPC::VSwitch::VSwitchId",
        "VSwitch",
        output_kind="resource_id",
        resource_type="ALIYUN::VPC::VSwitch",
        value_pattern=r"^vsw-[A-Za-z0-9][A-Za-z0-9._-]*$",
        metadata=metadata_schema(
            {
                "RegionId": REGION,
                "ZoneId": string_schema(maxLength=128),
                "VPCId": string_schema(maxLength=128),
                "VpcId": string_schema(maxLength=128),
                "InstanceType": {
                    "oneOf": [string_schema(), {"type": "array", "maxItems": 50, "items": string_schema()}],
                    "affects": ["query_filter"],
                },
                "SAENamespaceId": string_schema(maxLength=256),
                "SecurityGroupId": string_schema(maxLength=128),
            }
        ),
        operations=(
            QueryOperation(
                "vpc.instance_type.available",
                "ecs",
                "DescribeAvailableResource",
                fixed_parameters=(("DestinationResource", "InstanceType"),),
                api_version="2014-05-26",
            ),
            QueryOperation(
                "vpc.vswitch.dataapi.ecs.describesecuritygroups",
                "ecs",
                "DescribeSecurityGroups",
                metadata_parameters=(("RegionId", "RegionId"), ("SecurityGroupId", "SecurityGroupId")),
                fixed_parameters=(("PageNumber", 1), ("PageSize", 20)),
                api_version="2014-05-26",
            ),
            QueryOperation(
                "vpc.vswitch.dataapi.serverless.describenamespaceresources",
                "sae",
                "DescribeNamespaceResources",
                metadata_parameters=(("SAENamespaceId", "NamespaceId"),),
                api_version="2019-05-06",
            ),
            QueryOperation(
                "vpc.vswitch.list",
                "vpc",
                "DescribeVSwitches",
                ("PageNumber", "PageSize", "VSwitchName"),
                (("ZoneId", "ZoneId"), ("VpcId", "VpcId")),
                api_version="2016-04-28",
            ),
        ),
    ),
    enabled_profile(
        "rds.instance",
        "ALIYUN::RDS::Instance::InstanceId",
        "RDS 实例",
        output_kind="resource_id",
        resource_type="ALIYUN::RDS::DBInstance",
        value_pattern=r"^rm-[A-Za-z0-9][A-Za-z0-9._-]*$",
        metadata=metadata_schema({"RegionId": REGION, "ZoneId": string_schema(), "VpcId": string_schema()}),
        operations=(
            QueryOperation(
                "rds.instance.list",
                "rds",
                "DescribeDBInstances",
                ("PageNumber", "PageSize", "SearchKey"),
                (("ZoneId", "ZoneId"), ("VpcId", "VpcId")),
                api_version="2014-08-15",
            ),
        ),
    ),
    enabled_profile(
        "oss.bucket",
        "ALIYUN::OSS::Bucket::BucketName",
        "OSS Bucket",
        output_kind="resource_name",
        resource_type="ALIYUN::OSS::Bucket",
        value_pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
        metadata=metadata_schema({"RegionId": REGION, "ShowRegionSelector": {**boolean_schema(), "const": False}}),
        operations=(
            QueryOperation("oss.bucket.list", "oss", "ListBuckets", ("prefix", "marker"), api_version="2019-05-17"),
        ),
    ),
    enabled_profile(
        "oss.bucket_object",
        "ALIYUN::OSS::Bucket::Object",
        "OSS 对象",
        description="在同一个选择界面中先选择 OSS Bucket，再选择其中一个 Object",
        output_kind="resource_name",
        resource_type="ALIYUN::OSS::Object",
        search_terms=("OSS object", "OSS bucket object", "OSS 对象", "Bucket Object"),
        interaction_kind="hierarchical",
        interaction_steps=("select_bucket", "select_object"),
        usage_hint=(
            "调用一次 select_cloud_resource；界面内部先选择 Bucket，再选择 Object。"
            "不要预先调用 oss.bucket，也不要传 BucketName 或 source。"
        ),
        value_pattern=r"^oss://[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/.+$",
        metadata=metadata_schema(
            {
                "RegionId": REGION,
                "ObjectType": string_schema(enum=["file", "dir", "other"], default="other", affects="ui_behavior"),
                "ValueType": {
                    **string_schema(enum=["OSSUrl", "ObjectName"], affects="ui_behavior"),
                    "const": "OSSUrl",
                    "default": "OSSUrl",
                },
                "Mode": {**string_schema(affects="ui_behavior"), "const": "select", "default": "select"},
                "ShowRegionSelector": {**boolean_schema(), "const": False},
                "Multiple": {**boolean_schema(), "const": False},
                "Prefix": string_schema(),
                "Delimiter": string_schema(),
                "MaxNumber": {"type": "number", "const": 1, "default": 1, "affects": ["ui_behavior"]},
                "ShowUpload": {**boolean_schema(), "const": False},
                "UploadFileMetadata": {
                    "type": "object",
                    "additionalProperties": False,
                    "maxProperties": 6,
                    "properties": {
                        "Directory": boolean_schema(),
                        "Multiple": {**boolean_schema(), "const": False},
                        "MaxCount": {"type": "number", "maximum": 1, "affects": ["ui_behavior"]},
                        "AcceptFileSuffixes": {
                            "type": "array",
                            "maxItems": 100,
                            "items": string_schema(),
                            "affects": ["ui_behavior"],
                        },
                        "AddSuffix": boolean_schema(),
                        "SuffixFormat": string_schema(affects="ui_behavior"),
                    },
                    "affects": ["ui_behavior"],
                },
            }
        ),
        operations=(
            QueryOperation("oss.bucket.list", "oss", "ListBuckets", ("prefix", "marker"), api_version="2019-05-17"),
            QueryOperation(
                "oss.object.list",
                "oss",
                "ListObjects",
                ("BucketName", "prefix", "delimiter", "marker", "max-keys"),
                api_version="2019-05-17",
            ),
        ),
    ),
    enabled_profile(
        "redis.instance",
        "ALIYUN::Redis::Instance::InstanceId",
        "Redis 实例",
        output_kind="resource_id",
        resource_type="ALIYUN::REDIS::Instance",
        value_pattern=r"^r-[A-Za-z0-9][A-Za-z0-9._-]*$",
        metadata=metadata_schema(
            {
                "RegionId": REGION,
                "InstanceType": string_schema(),
                "ChargeType": string_schema(),
                "EditionType": string_schema(),
                "InstanceClass": string_schema(),
                "NetworkType": string_schema(),
                "InstanceStatus": string_schema(),
                "VpcId": string_schema(),
            }
        ),
        operations=(
            QueryOperation(
                "redis.instance.list",
                "r-kvstore",
                "DescribeInstances",
                ("PageNumber", "PageSize", "SearchKey"),
                (
                    ("InstanceType", "InstanceType"),
                    ("ChargeType", "ChargeType"),
                    ("EditionType", "EditionType"),
                    ("InstanceClass", "InstanceClass"),
                    ("NetworkType", "NetworkType"),
                    ("InstanceStatus", "InstanceStatus"),
                    ("VpcId", "VpcId"),
                ),
                api_version="2015-01-01",
            ),
        ),
    ),
    enabled_profile(
        "redis.connection_url",
        "ALIYUN::Redis::Instance::ConnectionURL",
        "Redis 连接地址",
        output_kind="endpoint",
        resource_type=None,
        value_pattern=r"^redis://[^\s/:]+:\d{1,5}$",
        metadata=metadata_schema({"RegionId": REGION, "InstanceId": string_schema()}),
        source_selector_id="redis.instance",
        source_parameter_key="InstanceId",
        selection_kind="cloud_resource_derived_value",
        description="从一个 Redis 实例选择一个非秘密连接地址",
        operations=(
            QueryOperation(
                "redis.connection.list",
                "r-kvstore",
                "DescribeDBInstanceNetInfo",
                api_version="2015-01-01",
            ),
        ),
    ),
)


# Complete semantic inventory from design section 14.  The generated catalog
# keeps every audited entry and its fixed-operation evidence, including entries
# that are intentionally not advertised by the runtime.
RESOURCE_PROPERTIES: tuple[tuple[str, str, str, OutputKind], ...] = (
    ("ecs.managed_instance", "ALIYUN::ECS::ManagedInstance::InstanceId", "ECS 托管实例", "resource_id"),
    ("ecs.disk", "ALIYUN::ECS::Disk::DiskId", "ECS 云盘", "resource_id"),
    ("ecs.image", "ALIYUN::ECS::Image::ImageId", "ECS 镜像", "resource_id"),
    ("ecs.security_group", "ALIYUN::ECS::SecurityGroup::SecurityGroupId", "ECS 安全组", "resource_id"),
    ("ecs.snapshot", "ALIYUN::ECS::Snapshot::SnapshotId", "ECS 快照", "resource_id"),
    ("ecs.command", "ALIYUN::ECS::Command::CommandId", "云助手命令", "resource_id"),
    ("ecs.key_pair", "ALIYUN::ECS::KeyPair::KeyPairName", "ECS 密钥对", "resource_name"),
    ("ecs.auto_snapshot_policy", "ALIYUN::ECS::Snapshot::AutoSnapshotPolicyId", "自动快照策略", "resource_id"),
    ("ecs.resource_group", "ALIYUN::ECS::ResourceGroup::ResourceGroupId", "资源组", "resource_id"),
    ("ecs.deployment_set", "ALIYUN::ECS::DeploymentSet::DeploymentSetId", "部署集", "resource_id"),
    ("ecs.launch_template", "ALIYUN::ECS::LaunchTemplate::LaunchTemplateId", "启动模板", "resource_id"),
    ("ecs.vswitch", "ALIYUN::ECS::VSwitch", "VSwitch", "resource_id"),
    ("vpc.vpc", "ALIYUN::ECS::VPC::VPCId", "VPC", "resource_id"),
    ("vpc.route_table", "ALIYUN::VPC::VirtualBorderRouter::RouteTableId", "路由表", "resource_id"),
    ("vpc.nat_gateway", "ALIYUN::VPC::NatGateway::NatGatewayId", "NAT 网关", "resource_id"),
    ("vpc.eip", "ALIYUN::VPC::EIP::AllocationId", "弹性公网 IP", "resource_id"),
    (
        "privatelink.endpoint_service",
        "ALIYUN::PrivateLink::VpcEndpointService::ServiceId",
        "终端节点服务",
        "resource_id",
    ),
    ("cen.instance", "ALIYUN::CEN::Instance::CenId", "云企业网实例", "resource_id"),
    ("cen.transit_router", "ALIYUN::CEN::TransitRouter::TransitRouterId", "转发路由器", "resource_id"),
    ("slb.load_balancer", "ALIYUN::SLB::LoadBalancer::LoadBalancerId", "SLB 负载均衡", "resource_id"),
    ("slb.acl", "ALIYUN::SLB::ACL::ACLId", "SLB ACL", "resource_id"),
    ("slb.certificate", "ALIYUN::SLB::Certificate::CertificateId", "SLB 证书", "resource_id"),
    ("nlb.load_balancer", "ALIYUN::NLB::LoadBalancer::LoadBalancerId", "NLB 负载均衡", "resource_id"),
    ("nlb.server_group", "ALIYUN::NLB::ServerGroup::ServerGroupId", "NLB 服务器组", "resource_id"),
    ("nlb.security_policy", "ALIYUN::NLB::SecurityPolicy::SecurityPolicyId", "NLB 安全策略", "resource_id"),
    ("alb.load_balancer", "ALIYUN::ALB::LoadBalancer::LoadBalancerId", "ALB 负载均衡", "resource_id"),
    ("alb.acl", "ALIYUN::ALB::ACL::ACLId", "ALB ACL", "resource_id"),
    ("cas.certificate", "ALIYUN::CAS::Certificate::CertificateId", "数字证书", "resource_id"),
    (
        "ess.scaling_configuration",
        "ALIYUN::ESS::ScalingConfiguration::ScalingConfigurationId",
        "伸缩配置",
        "resource_id",
    ),
    ("ess.scaling_group", "ALIYUN::ESS::AutoScalingGroup::AutoScalingGroupId", "伸缩组", "resource_id"),
    (
        "ess.eci_scaling_configuration",
        "ALIYUN::ESS::ECIScalingConfiguration::ScalingConfigurationId",
        "ECI 伸缩配置",
        "resource_id",
    ),
    ("eci.container_group", "ALIYUN::ECI::ContainerGroup::ContainerGroupId", "ECI 容器组", "resource_id"),
    ("cs.cluster", "ALIYUN::CS::Cluster::ClusterId", "容器集群", "resource_id"),
    ("cs.node_pool", "ALIYUN::CS::Cluster::ClusterNodePool", "节点池", "resource_id"),
    ("sae.namespace", "ALIYUN::SAE::Namespace::NamespaceId", "SAE 命名空间", "resource_id"),
    ("polardb.cluster", "ALIYUN::POLARDB::DBCluster::DBClusterId", "PolarDB 集群", "resource_id"),
    ("gpdb.instance", "ALIYUN::GPDB::DBInstance::InstanceId", "AnalyticDB PostgreSQL 实例", "resource_id"),
    ("kafka.instance", "ALIYUN::Kafka::Instance::InstanceId", "Kafka 实例", "resource_id"),
    ("emr.cluster", "ALIYUN::Emr::ECSCluster::ClusterId", "EMR 集群", "resource_id"),
    ("lindorm.instance", "ALIYUN::Lindorm::Instance::InstanceId", "Lindorm 实例", "resource_id"),
    ("hologres.instance", "ALIYUN::Hologres::Instance::InstanceId", "Hologres 实例", "resource_id"),
    ("dashvector.cluster", "ALIYUN::DashVector::Cluster::ClusterName", "DashVector 集群", "resource_name"),
    ("eas.resource", "ALIYUN::EAS::Resource::ResourceId", "EAS 资源", "resource_id"),
    ("oss.object", "ALIYUN::OSS::Object::ObjectName", "OSS 对象", "resource_name"),
    ("nas.file_system", "ALIYUN::NAS::FileSystem::FileSystemId", "NAS 文件系统", "resource_id"),
    ("ehpc.file_system", "ALIYUN::EHPC::FileSystem::FileSystemId", "E-HPC 文件系统", "resource_id"),
    ("ehpc.cluster", "ALIYUN::EHPC::Cluster::ClusterId", "E-HPC 集群", "resource_id"),
    ("ram.user", "ALIYUN::RAM::User", "RAM 用户", "resource_name"),
    ("ram.role", "ALIYUN::RAM::Role", "RAM 角色", "resource_name"),
    ("ram.service_role", "ALIYUN::RAM::Service::Role", "RAM 服务角色", "resource_name"),
    ("ram.policy", "ALIYUN::RAM::Policy::PolicyName", "RAM 权限策略", "resource_name"),
    ("ecs.ram_role", "ALIYUN::ECS::RAM::Role", "ECS RAM 角色", "resource_name"),
    ("resource_manager.folder", "ALIYUN::ResourceManager::Folder", "资源夹", "resource_id"),
    ("resource_manager.account", "ALIYUN::ResourceManager::Account", "资源目录账号", "resource_id"),
    ("kms.key", "ALIYUN::KMS::Key::KeyId", "KMS 密钥", "resource_id"),
    ("kms.instance", "ALIYUN::KMS::Instance::InstanceId", "KMS 实例", "resource_id"),
    ("cloudsso.directory", "ALIYUN::CloudSSO::Directory::DirectoryId", "CloudSSO 目录", "resource_id"),
    ("cloudsso.user", "ALIYUN::CloudSSO::User::UserId", "CloudSSO 用户", "resource_id"),
    ("cloudsso.group", "ALIYUN::CloudSSO::Group::GroupId", "CloudSSO 用户组", "resource_id"),
    (
        "cloudsso.access_configuration",
        "ALIYUN::CloudSSO::AccessConfiguration::AccessConfigurationId",
        "CloudSSO 访问配置",
        "resource_id",
    ),
    ("cr.instance", "ALIYUN::CR::Instance::InstanceId", "企业版容器镜像实例", "resource_id"),
    ("cr.namespace", "ALIYUN::CR::NameSpace::Name", "容器镜像命名空间", "resource_name"),
    ("cr.repository", "ALIYUN::CR::Repository::RepoName", "容器镜像仓库", "resource_name"),
    ("acr.repo_attribute", "ALIYUN::ACR::Repo::RepoAttribute", "个人版容器镜像仓库", "endpoint"),
    ("acr.namespace", "ALIYUN::ACR::Namespace::Name", "个人版容器镜像命名空间", "resource_name"),
    ("fc.service", "ALIYUN::FC::Service::ServiceName", "函数计算服务", "resource_name"),
    ("fc.function", "ALIYUN::FC::Function::FunctionName", "函数计算函数", "resource_name"),
    ("fc3.function", "ALIYUN::FC3::Function::FunctionName", "函数计算 3.0 函数", "resource_name"),
    (
        "computenest.service_instance",
        "ALIYUN::ComputeNest::ServiceInstance::ServiceInstanceId",
        "计算巢服务实例",
        "resource_id",
    ),
    ("computenest.service", "ALIYUN::ComputeNest::Service::ServiceId", "计算巢服务", "resource_id"),
    ("computenest.artifact", "ALIYUN::ComputeNest::Artifact::ArtifactId", "计算巢部署物", "resource_id"),
    ("nest.service", "ALIYUN::NEST::Service::ServiceId", "计算巢服务", "resource_id"),
    (
        "service_catalog.portfolio",
        "ALIYUN::ServiceCatalog::LaunchOption::PortfolioId",
        "服务目录产品组合",
        "resource_id",
    ),
    (
        "service_catalog.product_version",
        "ALIYUN::ServiceCatalog::ProductVersion::ProductVersionId",
        "服务目录产品版本",
        "resource_id",
    ),
    ("flow.connection", "ALIYUN::Flow::Connection::ConnectionId", "云工作流连接", "resource_id"),
    ("flow.organization", "ALIYUN::Flow::Organization::OrganizationId", "云工作流组织", "resource_id"),
    ("eds.bundle", "ALIYUN::EDS::Bundle::BundleId", "云桌面模板", "resource_id"),
    ("eds.office_site", "ALIYUN::EDS::OfficeSite::OfficeSiteId", "云桌面工作区", "resource_id"),
    ("eds.policy_group", "ALIYUN::EDS::PolicyGroup::PolicyGroupId", "云桌面策略", "resource_id"),
    ("appflow.user_auth_config", "ALIYUN::AppFlow::UserAuthConfig::ConfigId", "AppFlow 用户认证配置", "resource_id"),
    ("apig.gateway", "ALIYUN::APIG::Gateway::GatewayId", "API 网关", "resource_id"),
    ("swas.instance", "ALIYUN::SWAS::Instance::InstanceId", "轻量应用服务器", "resource_id"),
    ("cms.workspace", "ALIYUN::CMS::Workspace", "云监控工作空间", "resource_id"),
    ("domain.domain", "ALIYUN::Domain::DomainName", "域名", "resource_name"),
    ("oos.template", "ALIYUN::OOS::Template::TemplateName", "OOS 模板", "resource_name"),
    ("oos.parameter", "ALIYUN::OOS::Parameter::Value", "OOS 参数", "resource_name"),
    ("oos.secret_parameter", "ALIYUN::OOS::SecretParameter::Value", "OOS 加密参数引用", "secret_reference"),
    ("oos.package", "ALIYUN::OOS::Package::PackageName", "OOS 软件包", "resource_name"),
    ("oos.application", "ALIYUN::OOS::Application::ApplicationName", "OOS 应用", "resource_name"),
    ("oos.application_group", "ALIYUN::OOS::ApplicationGroup::ApplicationGroupName", "OOS 应用分组", "resource_name"),
    ("oos.git_account", "ALIYUN::OOS::GitAccount::Name", "OOS Git 账号", "resource_name"),
    ("oos.git_organization", "ALIYUN::OOS::GitOrganization::Name", "OOS Git 组织", "resource_name"),
    ("oos.git_repository", "ALIYUN::OOS::GitRepository::Name", "OOS Git 仓库", "resource_name"),
    ("oos.git_branch", "ALIYUN::OOS::GitBranch::Name", "OOS Git 分支", "resource_name"),
    ("oos.patch_baseline", "ALIYUN::OOS::PatchBaseline::PatchBaselineName", "OOS 补丁基线", "resource_name"),
)


# DashVector's Centaur console API currently has only an intranet endpoint.
# Keep its audited ORE contract in the inventory, but do not expose a selector
# that the Web/Desktop public BFF cannot execute.
_DISABLED_SELECTOR_REASONS = {
    "dashvector.cluster": "public_endpoint_unavailable",
}


DERIVED_PROPERTIES: tuple[tuple[str, str, str, OutputKind, str], ...] = (
    (
        "ecs.launch_template_version",
        "ALIYUN::ECS::LaunchTemplate::LaunchTemplateVersion",
        "启动模板版本",
        "version",
        "ecs.launch_template",
    ),
    (
        "ess.eci_container",
        "ALIYUN::ESS::ECIScalingConfiguration::ContainerName",
        "ECI 容器名称",
        "derived_value",
        "ess.eci_scaling_configuration",
    ),
    ("oss.object_version", "ALIYUN::OSS::Object::ObjectVersionId", "OSS 对象版本", "version", "oss.object"),
    ("nas.mount_target", "ALIYUN::NAS::FileSystem::MountTargetDomain", "NAS 挂载地址", "endpoint", "nas.file_system"),
    (
        "ehpc.mount_target",
        "ALIYUN::EHPC::FileSystem::MountTargetDomain",
        "E-HPC 挂载地址",
        "endpoint",
        "ehpc.file_system",
    ),
    ("cr.repository_tag", "ALIYUN::CR::Repository::Tag", "容器镜像标签", "version", "cr.repository"),
    ("acr.repository_tag", "ALIYUN::ACR::Repo::Tag", "个人版容器镜像标签", "version", "acr.repo_attribute"),
    (
        "computenest.artifact_version",
        "ALIYUN::ComputeNest::Artifact::ArtifactIdVersion",
        "部署物版本",
        "version",
        "computenest.artifact",
    ),
    (
        "computenest.service_version",
        "ALIYUN::ComputeNestSupplier::Service::ServiceVersion",
        "计算巢服务版本",
        "version",
        "computenest.service",
    ),
    ("apig.domain", "ALIYUN::APIG::Gateway::ListDomains", "API 网关域名", "endpoint", "apig.gateway"),
    ("oos.template_version", "ALIYUN::OOS::Template::TemplateVersion", "OOS 模板版本", "version", "oos.template"),
    ("oos.package_version", "ALIYUN::OOS::Package::PackageVersion", "OOS 软件包版本", "version", "oos.package"),
    ("oos.deploy_revision", "ALIYUN::OOS::Application::DeployRevisionId", "OOS 部署修订", "version", "oos.application"),
    ("oos.git_commit", "ALIYUN::OOS::GitBranch::CommitHash", "Git Commit", "version", "oos.git_branch"),
    ("oos.git_latest_commit", "ACS::OOS::GitBranch::LatestCommitId", "Git 最新 Commit", "version", "oos.git_branch"),
)


_REQUIRED_METADATA: dict[str, tuple[str, ...]] = {
    "cen.transit_router": ("CenId",),
    "ess.scaling_configuration": ("ScalingGroupId",),
    "ess.eci_scaling_configuration": ("ScalingGroupId",),
    "cs.node_pool": ("ClusterId",),
    "oss.object": ("BucketName",),
    "cloudsso.user": ("DirectoryId",),
    "cloudsso.group": ("DirectoryId",),
    "cloudsso.access_configuration": ("DirectoryId",),
    "ram.service_role": ("Service",),
    "cr.namespace": ("InstanceId",),
    "cr.repository": ("InstanceId",),
    "fc.function": ("ServiceName",),
    "service_catalog.portfolio": ("ProductId",),
    "service_catalog.product_version": ("ProductId",),
    "flow.connection": ("organizationId", "sericeConnectionType"),
    "appflow.user_auth_config": ("ConnectorId", "ConnectorVersion", "AuthType"),
    "oos.application_group": ("ApplicationName",),
    "oos.git_account": ("Platform",),
    "oos.git_organization": ("Platform", "Owner"),
    "oos.git_repository": ("Platform", "Owner"),
    "oos.git_branch": ("Platform", "Owner", "RepoFullName"),
    "ecs.launch_template_version": ("LaunchTemplateId",),
    "ess.eci_container": ("ScalingGroupId", "ScalingConfigurationId"),
    "oss.object_version": ("BucketName", "ObjectName"),
    "nas.mount_target": ("FileSystemId",),
    "ehpc.mount_target": ("VolumeId",),
    "cr.repository_tag": ("InstanceId", "RepoName", "RepoNamespaceName"),
    "acr.repository_tag": ("RepoName", "RepoNamespace"),
    "computenest.artifact_version": ("ArtifactId",),
    "computenest.service_version": ("ServiceId",),
    "apig.domain": ("GatewayId",),
    "oos.template_version": ("TemplateName",),
    "oos.package_version": ("TemplateName",),
    "oos.deploy_revision": ("ApplicationName",),
    "oos.git_commit": ("Platform", "Owner", "RepoFullName", "Branch"),
    "oos.git_latest_commit": ("Platform", "Owner", "RepoFullName", "Branch"),
}


_SOURCE_PARAMETER_KEYS: dict[str, str] = {
    "ecs.launch_template_version": "LaunchTemplateId",
    "ess.eci_container": "ScalingConfigurationId",
    # The ORE component reads ObjectName from metadata, but sends the selected
    # object as the OSS ListObjectVersions ``prefix`` query parameter.
    "oss.object_version": "prefix",
    "nas.mount_target": "FileSystemId",
    "ehpc.mount_target": "VolumeId",
    "cr.repository_tag": "RepoName",
    "acr.repository_tag": "RepoName",
    "computenest.artifact_version": "ArtifactId",
    "computenest.service_version": "ServiceId",
    "apig.domain": "GatewayId",
    "oos.template_version": "TemplateName",
    "oos.package_version": "TemplateName",
    "oos.deploy_revision": "ApplicationName",
    "oos.git_commit": "Branch",
    "oos.git_latest_commit": "Branch",
}


_SOURCE_OPERATION_PARAMETERS: dict[str, tuple[tuple[str, str | None], ...]] = {
    # This selector lists all file systems and locates VolumeId in the
    # response; VolumeId is not part of the public request contract.
    "ehpc.mount_target": (("ehpc.mount_target.dataapi.ehpc.listfilesystemwithmounttargets", None),),
    # RepoName resolves RepoId through GetRepository.  The other calls do not
    # accept RepoName and must stay fenced by that resolution chain.
    "cr.repository_tag": (
        ("cr.repository_tag.dataapi.cr.getinstance", None),
        ("cr.repository_tag.dataapi.cr.listrepotag", None),
    ),
    # ORE expands ServiceId into ComputeNest's repeated Filter fields.
    "computenest.service_version": (
        (
            "computenest.service_version.dataapi.computenest0521.listservices",
            "Filter.1.Value.1",
        ),
    ),
    # Metadata is PascalCase, but APIG's request contract is lower camel case.
    "apig.domain": (("apig.domain.dataapi.apig.listdomains", "gatewayId"),),
}


_VALUE_FIELD_OVERRIDES: dict[str, tuple[str, ...]] = {
    "vpc.route_table": ("VbrId", "RouteTableId"),
    "slb.certificate": ("ServerCertificateId", "CACertificateId"),
    "cs.cluster": ("cluster_id",),
    "cs.node_pool": ("nodepool_id",),
    "ehpc.cluster": ("Id",),
    "ecs.resource_group": ("Id", "ResourceGroupId"),
    "sae.namespace": ("NamespaceName",),
    "ram.user": ("UserName",),
    "ram.role": ("RoleName",),
    "ram.service_role": ("RoleName",),
    "ram.policy": ("PolicyName",),
    "ecs.ram_role": ("RoleName",),
    "resource_manager.folder": ("FolderId",),
    "resource_manager.account": ("AccountId",),
    "cms.workspace": ("workspaceName",),
    "kms.key": ("keyId", "KeyId"),
    "kms.instance": ("KmsInstanceId",),
    "acr.namespace": ("namespace",),
    "appflow.user_auth_config": ("AuthConfigId", "ConfigId"),
    "apig.gateway": ("gatewayId", "GatewayId"),
    "apig.domain": ("domainId", "DomainId"),
    "flow.connection": ("uuid", "ConnectionId"),
    "flow.organization": ("id", "OrganizationId"),
    "ecs.vswitch": ("VSwitchId",),
    "vpc.vpc": ("VpcId",),
    "cr.namespace": ("NamespaceName", "Namespace"),
    "oos.parameter": ("Name", "ParameterName", "Value"),
    "oos.secret_parameter": ("Name", "ParameterName", "Value"),
    "oos.application": ("Name",),
    "oos.application_group": ("Name",),
    "oos.deploy_revision": ("RevisionId",),
    "oos.git_account": ("Owner", "Name", "AccountName"),
    "oos.git_organization": ("OrgName", "Name", "Organization"),
    "oos.git_repository": ("FullName", "RepoId", "Name", "RepoFullName", "RepoName"),
    "oos.git_branch": ("Name", "Branch"),
    "oos.git_commit": ("Sha",),
    "oos.git_latest_commit": ("Sha",),
    "oss.bucket_object": ("Key", "Name"),
    "oss.object": ("Key",),
    "oss.object_version": ("VersionId",),
    "ehpc.mount_target": ("MountTargetDomain",),
    "ecs.launch_template_version": ("VersionNumber",),
    "ess.eci_container": ("Name",),
    "computenest.service_version": ("Version",),
    "ess.scaling_group": ("ScalingGroupId",),
    "gpdb.instance": ("DBInstanceId",),
    "dashvector.cluster": ("Name",),
    "oos.package": ("TemplateName",),
    "oos.patch_baseline": ("Name",),
    "computenest.artifact_version": ("ArtifactVersion",),
    "oos.package_version": ("TemplateVersion",),
}


_SEARCH_TERM_OVERRIDES: dict[str, tuple[str, ...]] = {
    "oos.git_account": (
        "OOS GitHub 账号",
        "OOS GitHub 授权账号",
        "已授权给 OOS 的 GitHub 账号",
        "已经授权给 OOS 的 GitHub 账号",
        "GitHub 账号 OOS 授权",
        "GitHub account authorized for OOS",
    ),
}


_SERVER_PRODUCT_TARGETS: dict[str, tuple[str, str | None]] = {
    "apig": ("APIG", "2024-03-27"),
    "resourcemanager": ("ResourceManager", "2020-03-31"),
    "acr": ("cr", "2016-06-07"),
    "alb": ("Alb", "2020-06-16"),
    "alikafka20190916": ("alikafka", "2019-09-16"),
    "appflow": ("appflow", "2023-09-04"),
    "cas20200407": ("cas", "2020-04-07"),
    "cbn": ("Cbn", "2017-09-12"),
    "cdn": ("Cdn", "2018-05-10"),
    # Keep the console-only alias explicit while its fixed Web/Desktop
    # transport is resolved; never infer a target from the selector name.
    "centaur-console": ("centaur-console", None),
    "clouddesktopnew": ("ecd", "2020-09-30"),
    "cloudsso": ("cloudsso", "2021-05-15"),
    "cms20240330": ("Cms", "2024-03-30"),
    "computenest": ("ComputeNest", "2021-06-01"),
    "computenest0521": ("ComputeNestSupplier", "2021-05-21"),
    "cr": ("cr", "2018-12-01"),
    "cs": ("CS", "2015-12-15"),
    "devops2020": ("devops", "2021-06-25"),
    "domain": ("Domain", "2018-01-29"),
    "eas": ("eas", "2021-07-01"),
    "eci": ("Eci", "2018-08-08"),
    "ecs": ("Ecs", "2014-05-26"),
    "ecs20160314": ("Ecs", "2016-03-14"),
    "ehpc": ("EHPC", "2018-04-12"),
    "emr_new": ("Emr", "2021-03-20"),
    "ess": ("Ess", "2014-08-28"),
    "fc-api": ("FC-Open", "2021-04-06"),
    "fc3-api": ("FC", "2023-03-30"),
    "gpdb": ("gpdb", "2016-05-03"),
    "hitsdb20200615": ("hitsdb", "2020-06-15"),
    "hologram_2022": ("Hologram", "2022-06-01"),
    "kms": ("Kms", "2016-01-20"),
    "nas": ("NAS", "2017-06-26"),
    "nlb": ("Nlb", "2022-04-30"),
    "oos": ("oos", "2019-06-01"),
    "oss": ("Oss", "2019-05-17"),
    "polardb": ("polardb", "2017-08-01"),
    "privatelink20200415": ("Privatelink", "2020-04-15"),
    "r-kvstore": ("R-kvstore", "2015-01-01"),
    "ram": ("Ram", "2015-05-01"),
    "rds": ("Rds", "2014-08-15"),
    "resource-directory": ("ResourceDirectoryMaster", "2022-04-19"),
    "resourcegroup": ("Ram", "2015-05-01"),
    "serverless": ("sae", "2019-05-06"),
    "servicecatalog": ("servicecatalog", "2021-09-01"),
    "slb": ("Slb", "2014-05-15"),
    "swas-open": ("SWAS-OPEN", "2020-06-01"),
    "vpc": ("Vpc", "2016-04-28"),
}

_SERVER_OPERATION_OVERRIDES: dict[str, tuple[str, str, str | None]] = {
    # ORE's /data/api.json product aliases are mapped to equivalent public,
    # read-only OpenAPI operations for Web/Desktop credentials.
    "ecs.vswitch.dataapi.serverless.describenamespaceresources": (
        "sae",
        "DescribeNamespaceResources",
        "2019-05-06",
    ),
    "sae.namespace.dataapi.serverless.listnamespacesv2": ("sae", "DescribeNamespaces", "2019-05-06"),
    "ram.service_role.dataapi.resourcegroup.listrolesforservice": ("Ram", "ListRolesForService", "2015-05-01"),
    "ecs.vswitch.dataapi.ecs20160314.describeresources": ("Vpc", "DescribeVSwitches", "2016-04-28"),
    "vpc.vpc.dataapi.ecs20160314.describeresources": ("Vpc", "DescribeVpcs", "2016-04-28"),
    # Personal-edition ACR uses the older, public ROA API.  These are not the
    # enterprise-edition 2018 API operations (which require InstanceId).
    "acr.repo_attribute.dataapi.acr.listrepo": ("cr", "GetRepoList", "2016-06-07"),
    "acr.namespace.dataapi.acr.listnamespace": ("cr", "GetNamespaceList", "2016-06-07"),
    "acr.repository_tag.dataapi.acr.listrepotag": ("cr", "GetRepoTags", "2016-06-07"),
    # ORE's console ``GetBucket`` wrapper is the bucket-scoped object listing,
    # not the OSS GetBucketInfo operation.
    "oss.object.dataapi.oss.getbucket": ("Oss", "ListObjects", "2019-05-17"),
}

_SERVER_TRANSPORT_OVERRIDES: dict[
    str,
    tuple[str | None, str | None, str | None, tuple[str, ...]],
] = {
    "acr.repo_attribute.dataapi.acr.listrepo": ("ROA", "GET", "/repos", ()),
    "acr.namespace.dataapi.acr.listnamespace": ("ROA", "GET", "/namespace", ()),
    "acr.repository_tag.dataapi.acr.listrepotag": (
        "ROA",
        "GET",
        "/repos/{RepoNamespace}/{RepoName}/tags",
        ("RepoNamespace", "RepoName"),
    ),
}

_SERVER_BODY_OPERATIONS = {
    # Hologres ListInstances is a JSON-style ROA operation whose filters are
    # carried by the request body; RegionId only selects the endpoint.
    "hologres.instance.dataapi.hologram_2022.listinstances",
}

_SERVER_FIXED_PARAMETERS: dict[str, tuple[tuple[str, Any], ...]] = {
    # Disk tags are a component-owned RepeatList.  The standalone selector
    # does not expose a tags input, so only its empty value may cross the BFF.
    "ecs.disk.dataapi.ecs.describedisks": (("Tag", []),),
    "cms.workspace.dataapi.cms20240330.listworkspaces": (("RegionId", "cn-hongkong"),),
    "ecs.vswitch.dataapi.ecs20160314.describeresources": (
        ("Product", "Vpc"),
        ("Global", "false"),
        ("ResourceType", "VSwitch"),
    ),
    "vpc.vpc.dataapi.ecs20160314.describeresources": (
        ("Product", "Vpc"),
        ("Global", "false"),
        ("ResourceType", "Instance"),
    ),
}

_SERVER_METADATA_PARAMETER_OVERRIDES: dict[str, tuple[tuple[str, str], ...]] = {
    # The CMS API uses lower-case ``region`` as the selected workspace region;
    # upper-case RegionId is the fixed endpoint-routing region used by ORE.
    "cms.workspace.dataapi.cms20240330.listworkspaces": (("RegionId", "region"),),
    # RepoId is runtime state obtained from the preceding trusted
    # GetRepository response, not caller-supplied AssociationPropertyMetadata.
    "cr.repository_tag.dataapi.cr.listrepotag": (
        ("RegionId", "RegionId"),
        ("InstanceId", "InstanceId"),
    ),
    # ORE derives the SAE API's NamespaceId from the selector metadata key
    # SAENamespaceId and adds the selected region prefix before querying.
    "ecs.vswitch.dataapi.serverless.describenamespaceresources": (("SAENamespaceId", "NamespaceId"),),
    # VpcId can be produced by the preceding SAE/security-group lookup.  It is
    # validated against the BFF's observed parent set instead of being treated
    # as caller-owned AssociationPropertyMetadata.
    "ecs.vswitch.dataapi.vpc.describevswitches": (
        ("RegionId", "RegionId"),
        ("ZoneId", "ZoneId"),
    ),
}

# Defaults declared by the concrete ORE selector components rather than by
# their minimal ``Metadata`` arrays.  Materializing them in the trusted
# selector context lets the BFF verify browser echoes against the same value
# the component uses while still allowing an explicit metadata override.
_SERVER_METADATA_DEFAULTS: dict[str, dict[str, Any]] = {
    "cas.certificate": {"OrderType": "CERT"},
    "cr.repository": {"RepoStatus": "ALL"},
    "eds.bundle": {
        "SupportMultiSession": True,
        "BundleType": "CUSTOM",
        "ProtocolType": "",
    },
    "emr.cluster": {
        "ClusterStates": [
            "BOOTSTRAPPING",
            "RUNNING",
            "STARTING",
            "START_FAILED",
            "TERMINATED_WITH_ERRORS",
            "TERMINATE_FAILED",
            "TERMINATING",
        ]
    },
    "oos.package": {"ShareType": "Public"},
    "oos.template": {"ShareType": "Public"},
    "oss.object": {"Delimiter": "/"},
    "service_catalog.product_version": {"Active": True},
}


def _metadata_entry_schema(entry: dict[str, Any]) -> dict[str, Any]:
    def with_constraints(schema: dict[str, Any], *names: str) -> dict[str, Any]:
        for name in names:
            if name in entry:
                schema[name] = entry[name]
        return schema

    kind = str(entry.get("type") or "string").lower()
    if kind == "boolean":
        return with_constraints(boolean_schema(), "default", "const")
    if kind == "number":
        return with_constraints(
            {"type": "number", "affects": ["query_filter"]},
            "enum",
            "default",
            "const",
            "minimum",
            "maximum",
        )
    if kind == "array":
        return with_constraints(
            {"type": "array", "maxItems": 100, "items": string_schema(), "affects": ["query_filter"]},
            "default",
            "minItems",
            "maxItems",
        )
    if kind == "object":
        nested = {
            str(item["key"]): _metadata_entry_schema(item)
            for item in entry.get("properties", [])
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "maxProperties": 32,
            "properties": nested,
            "affects": ["query_filter"],
        }
    if kind.startswith("dictionary"):
        return {
            "type": "object",
            "additionalProperties": string_schema(),
            "maxProperties": 32,
            "affects": ["query_filter"],
        }
    return with_constraints(
        string_schema(),
        "enum",
        "default",
        "const",
        "minLength",
        "maxLength",
        "pattern",
    )


def _catalog_profiles() -> tuple[SelectorProfile, ...]:
    root = Path(__file__).resolve().parent
    capabilities = json.loads((root / "ore-capabilities.json").read_text(encoding="utf-8"))["selectors"]
    operation_rows = json.loads((root / "ore-operations.json").read_text(encoding="utf-8"))["selectors"]
    operations_by_id = {row["selectorId"]: row["operations"] for row in operation_rows}
    baselines = {
        selector_id: (association_property, title, output_kind, None)
        for selector_id, association_property, title, output_kind in RESOURCE_PROPERTIES
    }
    baselines.update(
        {
            selector_id: (association_property, title, output_kind, source_selector_id)
            for selector_id, association_property, title, output_kind, source_selector_id in DERIVED_PROPERTIES
        }
    )
    profiles: list[SelectorProfile] = []
    for item in capabilities:
        selector_id = item["selectorId"]
        if selector_id in {profile.selector_id for profile in ENABLED_PROFILES}:
            continue
        association_property, title, output_kind, source_selector_id = baselines[selector_id]
        properties = {
            entry["key"]: (REGION if entry["key"] == "RegionId" else _metadata_entry_schema(entry))
            for entry in item["metadata"]
        }
        for metadata_key, default in _SERVER_METADATA_DEFAULTS.get(selector_id, {}).items():
            if metadata_key in properties:
                properties[metadata_key]["default"] = default
        required = list(_REQUIRED_METADATA.get(selector_id, ()))
        if "RegionId" in properties:
            required.insert(0, "RegionId")
        output_kind_by_attribute: dict[str, OutputKind] | None = None
        if association_property == "ALIYUN::ACR::Repo::RepoAttribute":
            properties["Attribute"] = string_schema(
                enum=["repoId", "repoName", "internalDomain", "publicDomain", "vpcDomain"]
            )
            output_kind_by_attribute = {
                "repoId": "resource_id",
                "repoName": "resource_name",
                "internalDomain": "endpoint",
                "publicDomain": "endpoint",
                "vpcDomain": "endpoint",
                "": "endpoint",
            }
        value_pattern_by_attribute = None
        if output_kind_by_attribute is not None:
            value_pattern_by_attribute = {
                attribute: _VALUE_PATTERNS_BY_OUTPUT_KIND[kind] for attribute, kind in output_kind_by_attribute.items()
            }
        if selector_id == "oss.object" and "ValueType" in properties:
            properties["ValueType"].update({"const": "ObjectName", "default": "ObjectName"})
        if selector_id == "domain.domain" and "ShowDomainPrefixInput" in properties:
            properties["ShowDomainPrefixInput"].update({"const": False, "default": False})
        operations = []
        for operation in operations_by_id[selector_id]:
            request_kind = operation.get("requestKind", "dataApi")
            allowed = tuple(operation.get("allowedParameters", ()))
            raw_product = operation["product"]
            product, api_version = _SERVER_PRODUCT_TARGETS.get(
                raw_product.lower(),
                (raw_product, None),
            )
            product, action, api_version = _SERVER_OPERATION_OVERRIDES.get(
                operation["key"],
                (product, operation["action"], api_version),
            )
            metadata_keys_by_lower = {key.lower(): key for key in properties}
            source_parameter_key = _SOURCE_PARAMETER_KEYS.get(selector_id)
            metadata_parameters = tuple(
                (metadata_keys_by_lower[name.lower()], name)
                for name in allowed
                if name.lower() in metadata_keys_by_lower and name != source_parameter_key
            )
            metadata_parameters = _SERVER_METADATA_PARAMETER_OVERRIDES.get(
                operation["key"],
                metadata_parameters,
            )
            metadata_api_parameters = {api_parameter for _, api_parameter in metadata_parameters}
            fixed_api_parameters = {
                api_parameter for api_parameter, _ in _SERVER_FIXED_PARAMETERS.get(operation["key"], ())
            }
            style, method, pathname_template, path_parameters = _SERVER_TRANSPORT_OVERRIDES.get(
                operation["key"],
                (None, None, None, ()),
            )
            operations.append(
                QueryOperation(
                    key=operation["key"],
                    product=product,
                    action=action,
                    dynamic_parameters=("requests",)
                    if request_kind == "multiApi"
                    else tuple(
                        name
                        for name in allowed
                        if name not in metadata_api_parameters
                        and name not in fixed_api_parameters
                        and name != source_parameter_key
                    ),
                    accepted_parameters=("requests",) if request_kind == "multiApi" else allowed,
                    metadata_parameters=metadata_parameters,
                    fixed_parameters=_SERVER_FIXED_PARAMETERS.get(operation["key"], ()),
                    request_kind=request_kind,
                    batch_parameters=allowed if request_kind == "multiApi" else (),
                    batch_dynamic_parameters=tuple(
                        name
                        for name in allowed
                        if name not in metadata_api_parameters
                        and name not in fixed_api_parameters
                        and name != source_parameter_key
                        and name != "requests"
                    )
                    if request_kind == "multiApi"
                    else (),
                    api_version=api_version,
                    style=style,
                    method=method,
                    pathname_template=pathname_template,
                    path_parameters=path_parameters,
                    parameters_in_body=operation["key"] in _SERVER_BODY_OPERATIONS,
                    response_projector=operation["key"],
                )
            )
        final_segment = association_property.rsplit("::", 1)[-1]
        unsupported_reason = _DISABLED_SELECTOR_REASONS.get(selector_id)
        profiles.append(
            SelectorProfile(
                selector_id=selector_id,
                association_property=association_property,
                title=title,
                description=("从一个源资源选择{}" if source_selector_id else "选择一个{}").format(title),
                selection_kind=item["selectionKind"],
                output_kind=output_kind,
                resource_type=None if source_selector_id else association_property.rsplit("::", 1)[0],
                metadata_schema=metadata_schema(properties, required=tuple(dict.fromkeys(required))),
                source_selector_id=source_selector_id,
                source_parameter_key=_SOURCE_PARAMETER_KEYS.get(selector_id),
                source_operation_parameters=_SOURCE_OPERATION_PARAMETERS.get(selector_id, ()),
                operations=tuple(operations),
                value_pattern=_VALUE_PATTERNS_BY_OUTPUT_KIND[output_kind],
                enabled=unsupported_reason is None,
                unsupported_reason=unsupported_reason,
                ore_association_property=item["oreAssociationProperty"]
                if item["oreAssociationProperty"] != association_property
                else None,
                output_kind_by_attribute=output_kind_by_attribute,
                value_pattern_by_attribute=value_pattern_by_attribute,
                value_fields=_VALUE_FIELD_OVERRIDES.get(selector_id, (final_segment,)),
                search_terms=_SEARCH_TERM_OVERRIDES.get(selector_id, ()),
                interaction_kind="derived" if source_selector_id else "single",
            )
        )
    return tuple(profiles)


OUT_OF_SCOPE: dict[str, SelectorProfile] = {
    "ALIYUN::ECS::ZoneId": SelectorProfile(
        selector_id="ecs.zone",
        association_property="ALIYUN::ECS::ZoneId",
        title="ECS 可用区",
        description="可用区属于目录值，不是单个云资源或派生值",
        selection_kind="cloud_resource",
        output_kind="resource_id",
        resource_type=None,
        metadata_schema=metadata_schema(
            {
                "RegionId": REGION,
                "SystemDiskCategory": string_schema(active_when={"DefaultValueStrategy": "diamond"}),
                "InstanceType": string_schema(active_when={"DefaultValueStrategy": "diamond"}),
                "AllowedValues": {"type": "array", "items": string_schema(), "affects": ["query_filter"]},
                "InstanceChargeType": string_schema(enum=["PrePaid", "PostPaid"]),
                "ShowRandom": boolean_schema(),
                "WithAvailableResource": boolean_schema(affects="query_filter"),
                "DefaultValueStrategy": string_schema(enum=["first", "random", "diamond"], affects="defaulting"),
            }
        ),
        unsupported_reason="catalog_value_out_of_scope",
    )
}

ALIASES = {
    "ALIYUN::ECS::Instance": "ALIYUN::ECS::Instance::InstanceId",
    "ALIYUN::ECS::Instance::ImageId": "ALIYUN::ECS::Image::ImageId",
    "ALIYUN::ECS::VSwitch::VSwitchId": "ALIYUN::VPC::VSwitch::VSwitchId",
    "ALIYUN::CAS::Certificate": "ALIYUN::CAS::Certificate::CertificateId",
    "ALIYUN::SLB::Instance::InstanceId": "ALIYUN::SLB::LoadBalancer::LoadBalancerId",
    "ALIYUN::NLB::Instance::InstanceId": "ALIYUN::NLB::LoadBalancer::LoadBalancerId",
    "ALIYUN::ALB::Instance::InstanceId": "ALIYUN::ALB::LoadBalancer::LoadBalancerId",
    "ALIYUN::NEST::Service::ServiceVersion": "ALIYUN::ComputeNestSupplier::Service::ServiceVersion",
    "ALIYUN::DomainName": "ALIYUN::Domain::DomainName",
    "OOSServiceRole": "ALIYUN::RAM::Service::Role",
}

PROFILES = (*ENABLED_PROFILES, *_catalog_profiles())
_BY_ID = {profile.selector_id: profile for profile in PROFILES}
_BY_PROPERTY = {profile.association_property: profile for profile in PROFILES}
if len(_BY_ID) != len(PROFILES) or len(_BY_PROPERTY) != len(PROFILES):
    raise RuntimeError("resource selector ids and AssociationProperty values must be unique")


def iter_profiles(*, include_disabled: bool = True) -> tuple[SelectorProfile, ...]:
    return PROFILES if include_disabled else tuple(profile for profile in PROFILES if profile.enabled)


def get_profile(selector_id: str) -> SelectorProfile | None:
    return _BY_ID.get(selector_id)


def get_profile_by_association_property(value: str, *, include_aliases: bool = False) -> SelectorProfile | None:
    canonical = ALIASES.get(value, value) if include_aliases else value
    return _BY_PROPERTY.get(canonical) or OUT_OF_SCOPE.get(canonical)


def is_out_of_scope(association_property: str) -> bool:
    return association_property in OUT_OF_SCOPE


def _hash_payload() -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for profile in PROFILES:
        if not profile.enabled:
            continue
        item = asdict(profile)
        item.pop("title", None)
        item.pop("description", None)
        item.pop("unsupported_reason", None)
        item.pop("search_terms", None)
        item.pop("interaction_kind", None)
        item.pop("interaction_steps", None)
        item.pop("usage_hint", None)
        payload.append(item)
    return payload


PROFILE_HASH = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(_hash_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
)
