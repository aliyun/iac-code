"""Reviewed resource locator facts, independent of console URLs and presentation.

This contract is shared by tools and local replay. Never persist arbitrary tool
metadata or complete cloud responses here. URL rules belong to the Web frontend.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

RESOURCE_REFERENCES_METADATA_KEY = "resource_references"
RESOURCE_REFERENCE_SCHEMA_VERSION = 1
EVIDENCE_LEVELS = {"type_rule": 0, "user_context": 1, "tool_input": 2, "api_response": 3}
_NON_RESOURCE_ID_SENTINELS = frozenset({"-", "n/a", "nil", "none", "null"})


@dataclass(frozen=True)
class ResourceSpec:
    resource_type: str
    identifier_field: str
    identifier_kind: str = "id"
    scope_kind: str = "regional"
    aliases: tuple[str, ...] = ()
    parent_fields: tuple[str, ...] = ()
    attribute_fields: tuple[str, ...] = ()
    stable_field: str = ""
    # Additional locators returned on the same reviewed API response object.
    # They identify the same resource while the primary locator remains the
    # value required by the console route (for example GroupName beside
    # GroupId).  Each tuple is (field, identifier kind).
    alias_fields: tuple[tuple[str, str], ...] = ()


# Identity contracts, not a promise of a verified console route for every type.
# RAM provider id is RoleName (resource_alicloud_ram_role.go, d.SetId).
# ROS RAM PhysicalResourceId semantics are deliberately left opaque until a
# RoleName attribute is available; a numeric RoleId must never become a name.
RESOURCE_SPECS = (
    ResourceSpec(
        "ALIYUN::SLS::Project",
        "projectName",
        "name",
        "account",
        aliases=("alicloud_log_project",),
    ),
    ResourceSpec("ALIYUN::ROS::Stack", "StackId", aliases=("alicloud_ros_stack",)),
    ResourceSpec("ALIYUN::ROS::StackGroup", "StackGroupId", attribute_fields=("StackGroupName",)),
    ResourceSpec("ALIYUN::ROS::TemplateScratch", "TemplateScratchId"),
    ResourceSpec(
        "ALIYUN::ACTIONTRAIL::Trail",
        "Name",
        "name",
        attribute_fields=("HomeRegion", "Name", "TrailArn"),
        stable_field="TrailArn",
    ),
    ResourceSpec(
        "ALIYUN::CR::Namespace",
        "NamespaceId",
        parent_fields=("InstanceId",),
        attribute_fields=("InstanceId", "NamespaceId", "NamespaceName"),
        alias_fields=(("NamespaceName", "name"),),
    ),
    ResourceSpec(
        "ALIYUN::CR::Repository",
        "RepoId",
        parent_fields=("InstanceId", "RepoNamespaceName"),
        attribute_fields=("InstanceId", "RepoId", "RepoName", "RepoNamespaceName"),
        alias_fields=(("RepoName", "name"),),
    ),
    ResourceSpec("ALIYUN::ElasticSearch::Instance", "instanceId"),
    ResourceSpec(
        "ALIYUN::KMS::Alias",
        "AliasName",
        "name",
        attribute_fields=("AliasArn", "AliasName", "KeyId"),
        stable_field="AliasArn",
    ),
    ResourceSpec("ALIYUN::ECS::Instance", "InstanceId", aliases=("alicloud_instance",)),
    ResourceSpec("ALIYUN::ECS::Disk", "DiskId", aliases=("alicloud_ecs_disk",)),
    ResourceSpec("ALIYUN::ECS::Snapshot", "SnapshotId", aliases=("alicloud_ecs_snapshot",)),
    ResourceSpec("ALIYUN::ECS::SecurityGroup", "SecurityGroupId", aliases=("alicloud_security_group",)),
    ResourceSpec(
        "ALIYUN::ECS::NetworkInterface",
        "NetworkInterfaceId",
        aliases=("alicloud_ecs_network_interface",),
    ),
    ResourceSpec("ALIYUN::ECS::SSHKeyPair", "KeyPairName", "name", aliases=("alicloud_ecs_key_pair",)),
    ResourceSpec("ALIYUN::VPC::VPC", "VpcId", aliases=("ALIYUN::ECS::VPC", "alicloud_vpc")),
    ResourceSpec("ALIYUN::VPC::VSwitch", "VSwitchId", aliases=("ALIYUN::ECS::VSwitch", "alicloud_vswitch")),
    ResourceSpec("ALIYUN::VPC::EIP", "AllocationId", aliases=("alicloud_eip_address",)),
    ResourceSpec(
        "ALIYUN::VPC::SnatEntry",
        "PhysicalResourceId",
        parent_fields=("NatGatewayId",),
        attribute_fields=("NatGatewayId", "SnatTableId", "NetworkType"),
    ),
    ResourceSpec(
        "ALIYUN::ECS::SNatEntry",
        "PhysicalResourceId",
        parent_fields=("NatGatewayId",),
        attribute_fields=("NatGatewayId", "SnatTableId", "NetworkType"),
    ),
    ResourceSpec(
        # The official Terraform clone/upgrade Read paths both resolve their
        # DBInstanceId through DescribeDBInstanceAttribute, not a new kind of
        # cloud object. Keep each route contract separate in acceptance.
        "ALIYUN::RDS::DBInstance",
        "DBInstanceId",
        aliases=("alicloud_db_instance", "alicloud_rds_clone_db_instance", "alicloud_rds_upgrade_db_instance"),
    ),
    ResourceSpec(
        # The Terraform parameter template is its own regional cloud object,
        # not a DB instance or the ROS instance-parameter attachment.
        "alicloud_rds_parameter_group",
        "ParameterGroupId",
        attribute_fields=("ParameterGroupId", "ParameterGroupName", "ParameterGroupType"),
    ),
    ResourceSpec(
        "ALIYUN::POLARDB::DBCluster",
        "PhysicalResourceId",
        aliases=("alicloud_polardb_cluster",),
        attribute_fields=("DBClusterId",),
    ),
    ResourceSpec(
        # The TF database is not the ROS DBInstance resource. Keep its own
        # catalog identity and the actual cluster that namespaces its name.
        "alicloud_polardb_database",
        "DBName",
        "name",
        parent_fields=("DBClusterId",),
        attribute_fields=("DBName", "DBClusterId"),
    ),
    ResourceSpec(
        "ALIYUN::POLARDB::DBClusterEndpoint",
        "DBEndpointId",
        aliases=("alicloud_polardb_endpoint",),
        parent_fields=("DBClusterId",),
        attribute_fields=("DBEndpointId", "DBClusterId"),
    ),
    ResourceSpec(
        "ALIYUN::POLARDB::DBClusterEndpointAddress",
        "ConnectionString",
        "name",
        aliases=("alicloud_polardb_endpoint_address",),
        parent_fields=("DBClusterId", "DBEndpointId"),
        attribute_fields=("ConnectionString", "DBClusterId", "DBEndpointId"),
    ),
    ResourceSpec(
        "ALIYUN::PrivateLink::VpcEndpoint",
        "PhysicalResourceId",
        aliases=("alicloud_privatelink_vpc_endpoint",),
        attribute_fields=("EndpointId",),
    ),
    ResourceSpec(
        "ALIYUN::PrivateLink::VpcEndpointService",
        "PhysicalResourceId",
        aliases=("alicloud_privatelink_vpc_endpoint_service",),
        attribute_fields=("ServiceId", "ServiceName"),
        alias_fields=(("ServiceId", "id"), ("ServiceName", "name")),
    ),
    ResourceSpec(
        "ALIYUN::POLARDB::Account",
        "AccountName",
        "name",
        aliases=("alicloud_polardb_account",),
        parent_fields=("DBClusterId",),
        attribute_fields=("AccountName", "DBClusterId"),
    ),
    ResourceSpec(
        "ALIYUN::RAM::Role",
        "RoleName",
        "name",
        "account",
        ("alicloud_ram_role",),
        attribute_fields=("RoleName", "RoleId"),
        stable_field="RoleId",
    ),
    ResourceSpec(
        "ALIYUN::RAM::User",
        "UserName",
        "name",
        "account",
        ("alicloud_ram_user",),
        attribute_fields=("UserName", "UserId"),
        stable_field="UserId",
    ),
    ResourceSpec(
        "ALIYUN::RAM::ManagedPolicy",
        "PhysicalResourceId",
        scope_kind="account",
        aliases=("alicloud_ram_policy",),
        attribute_fields=("PolicyName", "PolicyType"),
    ),
    ResourceSpec(
        "ALIYUN::RAM::Group",
        "PhysicalResourceId",
        scope_kind="account",
        aliases=("alicloud_ram_group",),
        attribute_fields=("GroupId", "GroupName"),
    ),
    ResourceSpec(
        "ALIYUN::PVTZ::Zone",
        "PhysicalResourceId",
        scope_kind="account",
        aliases=("alicloud_pvtz_zone",),
        attribute_fields=("ZoneId", "ZoneName"),
    ),
    ResourceSpec(
        "ALIYUN::PVTZ::ZoneRecord",
        "PhysicalResourceId",
        scope_kind="account",
        aliases=("alicloud_pvtz_zone_record",),
        parent_fields=("ZoneId",),
        attribute_fields=("RecordId", "ZoneId", "Rr", "Type", "Value"),
    ),
    ResourceSpec(
        "ALIYUN::OTS::Instance",
        "PhysicalResourceId",
        aliases=("alicloud_ots_instance",),
        attribute_fields=("InstanceName",),
    ),
    ResourceSpec(
        "ALIYUN::OTS::Table",
        "PhysicalResourceId",
        aliases=("alicloud_ots_table",),
        parent_fields=("InstanceName",),
        attribute_fields=("InstanceName", "TableName"),
    ),
    ResourceSpec(
        "ALIYUN::VPC::RouteTable",
        "PhysicalResourceId",
        aliases=("alicloud_route_table",),
        attribute_fields=("RouteTableId", "VpcId"),
    ),
    ResourceSpec(
        # Official TF SetId(ResourceGroup.Id); account-scoped, not a regional object.
        "ALIYUN::ResourceManager::ResourceGroup",
        "Id",
        "id",
        "account",
        aliases=("alicloud_resource_manager_resource_group",),
        attribute_fields=("DisplayName", "Id", "Name", "Status"),
    ),
    ResourceSpec(
        # Official TF SetId(response.RouterInterfaceId). RouterId is its parent,
        # never a replacement for the interface's identity.
        "ALIYUN::VPC::RouterInterface",
        "PhysicalResourceId",
        aliases=("alicloud_router_interface",),
        attribute_fields=("RouterInterfaceId", "RouterId", "RouterType"),
    ),
    ResourceSpec(
        # Terraform SetId(AppId) is the same application identity as the
        # reviewed ListApplications response, not a fabricated physical ID.
        "ALIYUN::SAE::Application",
        "AppId",
        aliases=("alicloud_sae_application",),
        attribute_fields=("AppId", "AppName", "AppType", "NamespaceName"),
    ),
    ResourceSpec(
        # Terraform SetId(NamespaceId) differs from the ROS NamespaceName
        # locator. Keep the full regional ID, including its short-ID suffix.
        "alicloud_sae_namespace",
        "NamespaceId",
        attribute_fields=("NamespaceId", "NamespaceName", "NameSpaceShortId", "RegionId"),
    ),
    ResourceSpec(
        "ALIYUN::ESS::LifecycleHook",
        "PhysicalResourceId",
        aliases=("alicloud_ess_lifecycle_hook",),
        attribute_fields=("LifecycleHookId", "ScalingGroupId"),
    ),
    ResourceSpec(
        "ALIYUN::ESS::ScalingConfiguration",
        "PhysicalResourceId",
        aliases=("alicloud_ess_scaling_configuration",),
        attribute_fields=("ScalingConfigurationId", "ScalingGroupId"),
    ),
    ResourceSpec("ALIYUN::OSS::Bucket", "BucketName", "name", aliases=("alicloud_oss_bucket",)),
    ResourceSpec(
        "ALIYUN::OOS::Template",
        "PhysicalResourceId",
        aliases=("alicloud_oos_template",),
        attribute_fields=("TemplateName", "TemplateId"),
    ),
    ResourceSpec("ALIYUN::SLB::LoadBalancer", "LoadBalancerId", aliases=("alicloud_slb_load_balancer",)),
    ResourceSpec(
        "ALIYUN::SLB::AccessControl",
        "AclId",
        aliases=("alicloud_slb_acl",),
        attribute_fields=("AclName", "AddressIPVersion"),
    ),
    ResourceSpec(
        "alicloud_slb_ca_certificate",
        "CACertificateId",
        attribute_fields=("CACertificateName", "Fingerprint", "CommonName"),
    ),
    ResourceSpec(
        # TF SetId(LoadBalancerId + ':' + protocol + ':' + frontend_port).
        # Do not turn a port alone into a globally unique listener ID.
        "alicloud_slb_listener",
        "ListenerId",
        parent_fields=("load_balancer_id",),
        attribute_fields=("LoadBalancerId", "ListenerPort", "ListenerProtocol", "Description"),
    ),
    ResourceSpec(
        "alicloud_slb_master_slave_server_group",
        "MasterSlaveServerGroupId",
        parent_fields=("LoadBalancerId",),
        attribute_fields=("MasterSlaveServerGroupName",),
    ),
    ResourceSpec(
        "alicloud_slb_rule",
        "RuleId",
        parent_fields=("LoadBalancerId", "ListenerPort", "ListenerProtocol"),
        attribute_fields=("RuleName", "Domain", "Url", "VServerGroupId"),
    ),
    ResourceSpec("ALIYUN::ALB::LoadBalancer", "LoadBalancerId", aliases=("alicloud_alb_load_balancer",)),
    ResourceSpec("ALIYUN::NLB::LoadBalancer", "LoadBalancerId", aliases=("alicloud_nlb_load_balancer",)),
    ResourceSpec("ALIYUN::MONGODB::Instance", "PhysicalResourceId", aliases=("alicloud_mongodb_instance",)),
    ResourceSpec("ALIYUN::REDIS::Instance", "InstanceId", aliases=("alicloud_kvstore_instance",)),
    ResourceSpec("ALIYUN::FC::Service", "ServiceName", "name", aliases=("alicloud_fc_service",)),
    ResourceSpec(
        "ALIYUN::FC::Function",
        "FunctionName",
        "name",
        aliases=("alicloud_fc_function",),
        parent_fields=("ServiceName",),
    ),
    ResourceSpec(
        "ALIYUN::FC::Trigger",
        "TriggerName",
        "name",
        aliases=("alicloud_fc_trigger",),
        parent_fields=("ServiceName", "FunctionName"),
    ),
    ResourceSpec(
        "ALIYUN::CEN::TransitRouter",
        "TransitRouterId",
        aliases=("alicloud_cen_transit_router",),
        parent_fields=("CenId",),
    ),
    ResourceSpec(
        "ALIYUN::CEN::TransitRouterVpcAttachment",
        "TransitRouterAttachmentId",
        aliases=("alicloud_cen_transit_router_vpc_attachment",),
        parent_fields=("CenId", "TransitRouterId"),
        attribute_fields=("VpcId",),
    ),
    ResourceSpec(
        "ALIYUN::ALB::Listener", "ListenerId", aliases=("alicloud_alb_listener",), parent_fields=("LoadBalancerId",)
    ),
    ResourceSpec(
        "ALIYUN::SLB::VServerGroup",
        "VServerGroupId",
        aliases=("alicloud_slb_server_group",),
        parent_fields=("LoadBalancerId",),
    ),
    ResourceSpec("ALIYUN::NAS::FileSystem", "FileSystemId", aliases=("alicloud_nas_file_system",)),
    ResourceSpec(
        "ALIYUN::SAG::ACLRule",
        "AcrId",
        aliases=("alicloud_sag_acl_rule",),
        parent_fields=("AclId",),
        attribute_fields=(
            "Name",
            "Direction",
            "Policy",
            "IpProtocol",
            "SourceCidr",
            "DestCidr",
            "SourcePortRange",
            "DestPortRange",
            "Priority",
        ),
    ),
    ResourceSpec(
        "ALIYUN::NAS::AccessGroup",
        "PhysicalResourceId",
        aliases=("alicloud_nas_access_group",),
        parent_fields=("FileSystemType",),
        attribute_fields=("AccessGroupName", "AccessGroupType", "FileSystemType"),
        alias_fields=(("AccessGroupName", "name"),),
    ),
    ResourceSpec(
        "ALIYUN::NAS::MountTarget",
        "MountTargetDomain",
        aliases=("alicloud_nas_mount_target",),
        parent_fields=("FileSystemId",),
    ),
    ResourceSpec("ALIYUN::NAS::DataFlow", "DataFlowId", parent_fields=("FileSystemId",)),
    ResourceSpec("ALIYUN::NAS::Fileset", "FilesetId", parent_fields=("FileSystemId",)),
    ResourceSpec(
        "ALIYUN::NAS::ProtocolMountTarget",
        "ProtocolMountTargetDomain",
        parent_fields=("FileSystemId", "ProtocolServiceId"),
        alias_fields=(("ExportId", "id"),),
    ),
    ResourceSpec("ALIYUN::NAS::ProtocolService", "ProtocolServiceId", parent_fields=("FileSystemId",)),
    ResourceSpec(
        "ALIYUN::ECS::Image",
        "ImageId",
        aliases=("alicloud_image",),
        attribute_fields=("ImageId", "DestinationRegionId"),
    ),
    ResourceSpec("ALIYUN::ECS::ImageCopy", "ImageId", attribute_fields=("ImageId", "DestinationRegionId")),
    ResourceSpec(
        "ALIYUN::ECS::ImageDetail",
        "PhysicalResourceId",
        parent_fields=("DestinationRegionId",),
    ),
)
_SPECS = {name: spec for spec in RESOURCE_SPECS for name in (spec.resource_type, *spec.aliases)}
_DIRECT_API_CATALOG_PATH = Path(__file__).parent / "aliyun" / "data" / "direct_resource_api_catalog.json"
_IDENTITY_SPECS: dict[str, ResourceSpec] | None = None
_CATALOG_SPECS: dict[str, ResourceSpec] | None = None
_DIRECT_ALIAS_FIELDS: dict[str, tuple[tuple[str, str], ...]] | None = None
_AUGMENTED_SPECS: dict[str, ResourceSpec] = {}
_SOURCE_FIELDS = (
    "tool_use_id",
    "message_id",
    "turn_id",
    "source_path",
    "request_region_id",
    "stack_id",
    "logical_resource_id",
    "action",
    "product",
    "evidence",
    "source_id",
    "query_source_digest",
    "query_parent_path",
)
_OPTIONAL_FIELDS = ("region_id", "account_scope", "console_partition", "logical_resource_id", "stack_id", "status")
SOURCE_VERSION_FIELDS = ("source_kind", "source_request_digest", "source_revision")


def _direct_alias_fields() -> dict[str, tuple[tuple[str, str], ...]]:
    """Derive conservative same-row ID/name locators from reviewed contracts.

    A response contract already proves the exact resource object path and its
    response locator field.  Preserve that field as an alias when a ROS-derived
    identity uses the generic ``PhysicalResourceId`` name, then also accept only
    the mechanically paired ``XxxName`` or ``XxxId`` field on that same object;
    arbitrary ID/name fields elsewhere in a response remain ignored.  The
    reverse pair matters for APIs such as OOS ListTemplates: the console route
    uses TemplateName, while a normal inventory response also displays
    TemplateId for the same template.
    """
    global _DIRECT_ALIAS_FIELDS
    if _DIRECT_ALIAS_FIELDS is None:
        data = json.loads(_DIRECT_API_CATALOG_PATH.read_text(encoding="utf-8"))
        identity_kinds = {
            item["resourceType"]: item["identifierKind"]
            for item in data.get("resources", [])
            if isinstance(item, dict)
            and isinstance(item.get("resourceType"), str)
            and item.get("identifierKind") in {"id", "name"}
        }
        identity_kinds.update({spec.resource_type: spec.identifier_kind for spec in RESOURCE_SPECS})
        reviewed_specs = {spec.resource_type: spec for spec in RESOURCE_SPECS}
        values: dict[str, set[tuple[str, str]]] = {}
        for item in data.get("responseContracts", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            resource_type = item.get("resourceType")
            identifier_field = item.get("identifierField")
            if not isinstance(resource_type, str) or not isinstance(identifier_field, str):
                continue
            reviewed = reviewed_specs.get(resource_type)
            if reviewed is not None and identifier_field == reviewed.identifier_field:
                identifier_kind = reviewed.identifier_kind
            elif reviewed is not None and identifier_field == reviewed.stable_field:
                identifier_kind = "id"
            elif identifier_field.endswith("Id") or identifier_field.endswith("ID"):
                identifier_kind = "id"
            elif identifier_field.endswith("Name"):
                identifier_kind = "name"
            else:
                identifier_kind = identity_kinds.get(resource_type, "id")
            values.setdefault(resource_type, set()).add((identifier_field, identifier_kind))
            match = re.fullmatch(r"(.*)Id", identifier_field)
            if match:
                values.setdefault(resource_type, set()).add((match.group(1) + "Name", "name"))
            match = re.fullmatch(r"(.*)Name", identifier_field)
            if match:
                values.setdefault(resource_type, set()).add((match.group(1) + "Id", "id"))
        # A few reviewed APIs use a short name that is not mechanically paired
        # with the ID field (EBS exposes ``GroupName`` beside
        # ``ReplicaGroupId``). Keep these overrides in the contract layer so
        # the same-row name is available without making a global name guess.
        overrides = {
            # MSE separates the billing/console InstanceId from ClusterId.
            # Both are returned on the same reviewed ListClusters row; the
            # instance remains primary while answers may display either.
            "ALIYUN::MSE::Cluster": (("ClusterId", "id"), ("ClusterAliasName", "name")),
            "ALIYUN::EBS::DiskReplicaGroup": (("GroupName", "name"),),
            # SmartAG list responses expose a human-readable ``Name`` beside
            # their product-specific ID fields.  Keep it tied to the same
            # reviewed response row so a generic name occurrence cannot be
            # claimed by an unrelated name-only resource (for example an
            # ActionTrail trail with the same display name).
            "ALIYUN::SAG::CloudConnectNetwork": (("Name", "name"),),
            "ALIYUN::SAG::SmartAccessGateway": (("Name", "name"),),
        }
        for resource_type, fields in overrides.items():
            values.setdefault(resource_type, set()).update(fields)
        _DIRECT_ALIAS_FIELDS = {resource_type: tuple(sorted(fields)) for resource_type, fields in values.items()}
    return _DIRECT_ALIAS_FIELDS


def _with_direct_alias_fields(spec: ResourceSpec) -> ResourceSpec:
    if cached := _AUGMENTED_SPECS.get(spec.resource_type):
        return cached
    fields = tuple(dict.fromkeys((*spec.alias_fields, *_direct_alias_fields().get(spec.resource_type, ()))))
    result = spec if fields == spec.alias_fields else replace(spec, alias_fields=fields)
    _AUGMENTED_SPECS[spec.resource_type] = result
    return result


def resource_spec(resource_type: Any) -> ResourceSpec | None:
    if not isinstance(resource_type, str):
        return None
    if spec := _SPECS.get(resource_type):
        return _with_direct_alias_fields(spec)
    global _IDENTITY_SPECS
    if _IDENTITY_SPECS is None:
        data = json.loads(_DIRECT_API_CATALOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("resources"), list):
            raise RuntimeError("invalid direct resource API identity catalog")
        generated: dict[str, ResourceSpec] = {}
        for item in data["resources"]:
            if not isinstance(item, dict):
                raise RuntimeError("invalid direct resource API identity entry")
            aliases = item.get("aliases")
            parents = item.get("parentFields")
            attributes = item.get("attributeFields", [])
            if not isinstance(aliases, list) or not all(isinstance(value, str) for value in aliases):
                raise RuntimeError("invalid direct resource API aliases")
            if not isinstance(parents, list) or not all(isinstance(value, str) for value in parents):
                raise RuntimeError("invalid direct resource API parent fields")
            if not isinstance(attributes, list) or not all(isinstance(value, str) for value in attributes):
                raise RuntimeError("invalid direct resource API attribute fields")
            spec = ResourceSpec(
                resource_type=item.get("resourceType"),
                identifier_field=item.get("identifierField"),
                identifier_kind=item.get("identifierKind"),
                scope_kind=item.get("scopeKind"),
                aliases=tuple(aliases),
                parent_fields=tuple(parents),
                attribute_fields=tuple(attributes),
            )
            if (
                not isinstance(spec.resource_type, str)
                or not spec.resource_type
                or not isinstance(spec.identifier_field, str)
                or not spec.identifier_field
                or spec.identifier_kind not in {"id", "name"}
                or spec.scope_kind not in {"regional", "account"}
            ):
                raise RuntimeError("invalid direct resource API identity fields")
            for name in (spec.resource_type, *spec.aliases):
                if name in generated:
                    raise RuntimeError("duplicate direct resource API identity")
                generated[name] = spec
        _IDENTITY_SPECS = generated
    if spec := _IDENTITY_SPECS.get(resource_type):
        return _with_direct_alias_fields(spec)
    global _CATALOG_SPECS
    if _CATALOG_SPECS is None:
        # Imported lazily so non-Web commands do not read the catalog unless an
        # unknown resource type actually appears in a reviewed resource fact.
        from iac_code.web.resource_link_catalog import resource_link_catalog

        catalog = resource_link_catalog()
        _CATALOG_SPECS = {
            item: ResourceSpec(item, "PhysicalResourceId", parent_fields=catalog.attributes[item])
            for item in catalog.resource_types
            if item not in _SPECS
        }
    spec = _CATALOG_SPECS.get(resource_type)
    return _with_direct_alias_fields(spec) if spec else None


def locator_text(value: Any) -> str | None:
    """Accept literal, complete locators, not masks, expressions or control text."""
    if not isinstance(value, str) or not value or len(value) > 1024 or value != value.strip():
        return None
    if re.search(r"[\x00-\x20\x7f*<>`]|\$[\w{(]|\.\.\.|…|://", value):
        return None
    return value


def resource_locator_text(value: Any, spec: ResourceSpec | None, field: str) -> str | None:
    """Preserve reviewed SAE/CMS names with spaces; keep other locators strict."""
    if (
        spec is not None
        and (
            spec.resource_type in {"ALIYUN::SAE::Namespace", "alicloud_sae_namespace"} and field == "NamespaceName"
            or spec.resource_type in {"ALIYUN::CMS::ContactGroup", "alicloud_cms_alarm_contact_group"}
            and field in {"Name", "PhysicalResourceId"}
        )
        and isinstance(value, str)
        and value == value.strip()
        and len(value) <= 1024
        and locator_text(value.replace(" ", "")) is not None
    ):
        return value
    return locator_text(value)


def resource_attributes(value: Any, spec: ResourceSpec) -> dict[str, str]:
    if isinstance(value, list):
        value = {
            row.get("ResourceAttributeKey"): row.get("ResourceAttributeValue")
            for row in value
            if isinstance(row, dict) and isinstance(row.get("ResourceAttributeKey"), str)
        }
    if not isinstance(value, dict):
        return {}

    def attribute_text(key: str) -> str | None:
        raw = value.get(key)
        # A literal DNS wildcard is data in the same record row, not a masked
        # resource identifier. Do not relax locator_text or other attributes.
        if (
            spec.resource_type == "ALIYUN::PVTZ::ZoneRecord"
            and key == "Rr"
            and (raw == "*" or isinstance(raw, str) and raw.startswith("*.") and locator_text(raw[2:]))
        ):
            return raw
        return resource_locator_text(raw, spec, key)

    return {
        key: text
        for key in (
            spec.identifier_field,
            *spec.parent_fields,
            *spec.attribute_fields,
            *(field for field, _kind in spec.alias_fields),
        )
        if (text := attribute_text(key)) is not None
    }


def sanitize_resource_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("provider", "aliyun") != "aliyun":
        return None
    spec = resource_spec(value.get("resource_type"))
    identifier = resource_locator_text(value.get("identifier"), spec, value.get("identifier_field", ""))
    if spec is None or identifier is None:
        return None
    field = value.get("identifier_field")
    kind = value.get("identifier_kind")
    permitted = {spec.identifier_field: spec.identifier_kind, "PhysicalResourceId": "id"}
    if spec.stable_field:
        permitted[spec.stable_field] = "id"
    permitted.update(spec.alias_fields)
    if not isinstance(field, str) or field not in permitted or permitted[field] != kind:
        return None
    if kind == "id" and identifier.casefold() in _NON_RESOURCE_ID_SENTINELS:
        return None
    evidence = value.get("evidence")
    if not isinstance(evidence, str) or evidence not in EVIDENCE_LEVELS:
        return None
    result: dict[str, Any] = {
        "provider": "aliyun",
        "resource_type": value["resource_type"],
        "identifier": identifier,
        "identifier_kind": kind,
        "identifier_field": field,
        "scope_kind": spec.scope_kind,
        "evidence": evidence,
        "attributes": resource_attributes(value.get("attributes"), spec),
        "aliases": [],
        "sources": [],
    }
    for key in _OPTIONAL_FIELDS:
        if text := locator_text(value.get(key)):
            result[key] = text
    if spec.scope_kind == "account":
        result.pop("region_id", None)
    if result.get("console_partition") not in {"aliyun", "alibabacloud"}:
        result.pop("console_partition", None)
    aliases = value.get("aliases")
    for alias in aliases if isinstance(aliases, list) else []:
        if not isinstance(alias, dict):
            continue
        alias_field = alias.get("identifier_field")
        if (
            isinstance(alias_field, str)
            and alias_field in permitted
            and alias_field != "PhysicalResourceId"
            and permitted.get(alias_field) == alias.get("identifier_kind")
            and (alias_text := resource_locator_text(alias.get("identifier"), spec, alias_field)) is not None
        ):
            candidate = {
                "identifier": alias_text,
                "identifier_kind": alias["identifier_kind"],
                "identifier_field": alias_field,
            }
            if candidate not in result["aliases"]:
                result["aliases"].append(candidate)
    if kind == "id" and any(
        alias["identifier_kind"] == "id"
        and alias["identifier_field"] == spec.identifier_field
        and alias["identifier"] != identifier
        for alias in result["aliases"]
    ):
        # A single ID-addressed resource cannot have two different values for
        # its canonical ID field.  Older Web snapshots could contain exactly
        # this shape after merging same-name rows; rejecting the whole record
        # lets normal message replay rebuild the distinct resources safely.
        return None
    sources = value.get("sources")
    for source in sources if isinstance(sources, list) else []:
        if not isinstance(source, dict):
            continue
        clean = {key: item for key in _SOURCE_FIELDS if isinstance(item := source.get(key), str) and len(item) <= 2048}
        # Web-owned provenance is accepted only as a complete version tuple.
        # The Web bridge overwrites tool-supplied versions before observing them.
        source_kind = source.get("source_kind")
        if (
            isinstance(source_kind, str)
            and source_kind in {"tool_result", "stack_progress", "attribute_query"}
            and all(
                isinstance(source.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", source[key])
                for key in SOURCE_VERSION_FIELDS[1:]
            )
        ):
            clean.update({key: source[key] for key in SOURCE_VERSION_FIELDS})
        if clean.get("evidence") not in EVIDENCE_LEVELS:
            clean["evidence"] = evidence
        observed_at = source.get("observed_at")
        if (
            isinstance(observed_at, (float, int))
            and not isinstance(observed_at, bool)
            and 0 <= observed_at <= 253402300799
            and math.isfinite(observed_at)
        ):
            clean["observed_at"] = observed_at
        if clean.get("tool_use_id") or clean.get("message_id") or clean.get("source_id"):
            if clean not in result["sources"]:
                result["sources"].append(clean)
    if value["resource_type"] == "alicloud_nas_access_group" and field == "PhysicalResourceId":
        if evidence != "api_response" or not any(
            source.get("product") == "ros"
            and source.get("action") in {"ListStackResources", "GetStackResource"}
            and source.get("evidence") == "api_response"
            for source in result["sources"]
        ):
            return None
        parts = identifier.split(":")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", parts[0])
            or parts[1] not in {"standard", "extreme", "cpfs"}
        ):
            return None
        locators = {"AccessGroupName": parts[0], "FileSystemType": parts[1]}
        if any(result["attributes"].get(key, item) != item for key, item in locators.items()):
            return None
        if any(
            alias["identifier_field"] == "AccessGroupName" and alias["identifier"] != parts[0]
            for alias in result["aliases"]
        ):
            return None
        result["attributes"].update(locators)
        name_alias = {"identifier": parts[0], "identifier_kind": "name", "identifier_field": "AccessGroupName"}
        if name_alias not in result["aliases"]:
            result["aliases"].append(name_alias)
    if value["resource_type"] == "alicloud_nas_mount_target" and (field == "PhysicalResourceId" or ":" in identifier):
        # Only the provider's ROS-observed import ID has this parent:domain
        # contract. Keep its literal primary ID; route fields are a projection,
        # not a guess from the domain prefix or a different NAS query result.
        if evidence != "api_response" or not any(
            source.get("product") == "ros"
            and source.get("action") in {"ListStackResources", "GetStackResource"}
            and source.get("evidence") == "api_response"
            for source in result["sources"]
        ):
            return None
        parts = identifier.split(":")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.nas\.aliyuncs\.com", parts[1])
        ):
            return None
        locators = {"FileSystemId": parts[0], "MountTargetDomain": parts[1]}
        if any(result["attributes"].get(key, item) != item for key, item in locators.items()):
            return None
        result["attributes"].update(locators)
        # Older facts mislabeled the entire composite as MountTargetDomain.
        # Normalize metadata on replay without changing the source or raw ID.
        result["identifier_field"] = "PhysicalResourceId"
    # Historical ListStackResources facts stored ROS RAM role names under the
    # generic PhysicalResourceId locator. CHANNEL_LINKS defines that same value
    # as /roles/{PhysicalId}; enrich only reviewed ROS API observations and keep
    # an all-numeric value opaque because it may be a RoleId.
    if (
        value["resource_type"] == "ALIYUN::RAM::Role"
        and field == "PhysicalResourceId"
        and not identifier.isdecimal()
        and any(
            source.get("product") == "ros"
            and source.get("action") in {"ListStackResources", "GetStackResource"}
            and source.get("evidence") == "api_response"
            for source in result["sources"]
        )
    ):
        role_name = {
            "identifier": identifier,
            "identifier_kind": spec.identifier_kind,
            "identifier_field": spec.identifier_field,
        }
        if role_name not in result["aliases"]:
            result["aliases"].append(role_name)
    return result if result["sources"] else None


def resource_reference_metadata(references: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": RESOURCE_REFERENCE_SCHEMA_VERSION, "items": references}


def resource_references_from_metadata(metadata: Any) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    value = metadata.get(RESOURCE_REFERENCES_METADATA_KEY)
    if not isinstance(value, dict) or value.get("schema_version") != RESOURCE_REFERENCE_SCHEMA_VERSION:
        return []
    items = value.get("items")
    if not isinstance(items, list):
        return []
    return [clean for item in items if (clean := sanitize_resource_reference(item)) is not None]


def stack_resource_reference(
    row: dict[str, Any],
    *,
    stack_id: str,
    region_id: str,
    tool_use_id: str,
    source_path: str,
    account_scope: str = "",
    console_partition: str = "",
    action: str = "ListStackResources",
) -> dict[str, Any] | None:
    resource_type = row.get("resource_type") or row.get("ResourceType")
    spec = resource_spec(resource_type)
    if spec is None:
        return None
    attributes = resource_attributes(row.get("attributes") or row.get("ResourceAttributes"), spec)
    identifier = attributes.get(spec.identifier_field)
    field, kind = spec.identifier_field, spec.identifier_kind
    if not identifier:
        identifier = row.get("physical_resource_id") or row.get("PhysicalResourceId")
        # Do not silently interpret an opaque ROS physical value as a role name.
        if kind == "name" and resource_type not in {
            "ALIYUN::ECS::SSHKeyPair",
            "alicloud_ecs_key_pair",
            "ALIYUN::OSS::Bucket",
            "alicloud_ram_role",
            "alicloud_oss_bucket",
        }:
            field, kind = "PhysicalResourceId", "id"
    if resource_type == "alicloud_nas_mount_target":
        # ResourceAttributes may contain the domain, but it is not the complete
        # Terraform identity. Preserve and validate the original physical ID.
        identifier = row.get("physical_resource_id") or row.get("PhysicalResourceId")
        field, kind = "PhysicalResourceId", "id"
    source: dict[str, Any] = {
        "tool_use_id": tool_use_id,
        "source_path": source_path,
        "request_region_id": region_id,
        "stack_id": stack_id,
        "logical_resource_id": row.get("name") or row.get("LogicalResourceId") or "",
        "action": action,
        "product": "ros",
        "evidence": "api_response",
    }
    if resource_type == "alicloud_ots_table" and isinstance(identifier, str):
        # The provider's observed SetId is instance:table. Preserve that
        # full primary locator and derive only its parent/table route fields.
        # Never apply this convention to native ROS or arbitrary name strings.
        parts = identifier.split(":")
        if (
            len(parts) != 2
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,15}", parts[0])
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", parts[1])
        ):
            return None
        locators = {"InstanceName": parts[0], "TableName": parts[1]}
        if any(attributes.get(key, value) != value for key, value in locators.items()):
            return None
        attributes.update(locators)
    if resource_type == "alicloud_nas_access_rule" and isinstance(identifier, str):
        # Terraform's ID is group:rule[:file-system-type], not a globally
        # unique rule ID. Standard resources use the documented two-part ID.
        # Bind the primary rule and its parents, rejecting conflicting facts.
        parts = identifier.split(":")
        if len(parts) not in {2, 3} or any(locator_text(part) is None for part in parts):
            return None
        group, rule = parts[:2]
        file_system_type = parts[2] if len(parts) == 3 else "standard"
        if file_system_type not in {"standard", "extreme"}:
            return None
        parents = {"AccessGroupName": group, "FileSystemType": file_system_type, "AccessRuleId": rule}
        if any(attributes.get(key, value) != value for key, value in parents.items()):
            return None
        attributes.update(parents)
        identifier = rule
    if "observed_at" in row:
        source["observed_at"] = row["observed_at"]
    aliases = []
    if spec.stable_field and (stable_id := attributes.get(spec.stable_field)):
        aliases.append({"identifier": stable_id, "identifier_field": spec.stable_field, "identifier_kind": "id"})
    return sanitize_resource_reference(
        {
            "resource_type": resource_type,
            "identifier": identifier,
            "identifier_field": field,
            "identifier_kind": kind,
            "attributes": attributes,
            "aliases": aliases,
            "account_scope": row.get("account_scope") or account_scope,
            "console_partition": row.get("console_partition") or console_partition,
            "region_id": attributes.get("DestinationRegionId") or region_id,
            "stack_id": stack_id,
            "logical_resource_id": source["logical_resource_id"],
            "status": row.get("status") or row.get("Status"),
            "evidence": "api_response",
            "sources": [source],
        }
    )
