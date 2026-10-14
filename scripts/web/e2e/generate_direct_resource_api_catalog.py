#!/usr/bin/env python3
"""Regenerate reviewed direct Alibaba Cloud resource-response contracts.

The generated file is consumed at runtime and by the opt-in live E2E.  Source
inputs are the ROS resource metadata repository (identity fields and aliases)
and ROS extension cleanup scanners (read-only discovery API response paths).
Neither source repository is required by an installed iac-code package.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_READ_ACTION = re.compile(r"(?:Describe|List|Query|Get|Search)[A-Za-z0-9]+")
_NAME_FIELDS = re.compile(r"(?:Name|Domain|Bucket|Queue|Topic|Project|Namespace)\Z")

# Cleanup package names and public OpenAPI product codes are not always equal.
_PRODUCTS = {
    "apigateway": "cloudapi",
    "acr": "cr",
    "datahub": "datahub",
    "directmail": "dm",
    "memcache": "r-kvstore",
    "mns": "mns-open",
    "mongodb": "dds",
    "dns": "alidns",
    "ots": "tablestore",
    "redis": "r-kvstore",
    "rocketmq": "ons",
    "rocketmq5": "rocketmq",
    "sag": "smartag",
    "tsdb": "hitsdb",
}

_NAMESPACES = {
    "acm": "ACM",
    "acr": "CR",
    "actiontrail": "ACTIONTRAIL",
    "adb": "ADB",
    "alb": "ALB",
    "apigateway": "ApiGateway",
    "asm": "ASM",
    "clickhouse": "ClickHouse",
    "cloudphone": "CloudPhone",
    "datahub": "DATAHUB",
    "directmail": "DirectMail",
    "elasticsearch": "ElasticSearch",
    "memcache": "MEMCACHE",
    "mongodb": "MONGODB",
    "privatelink": "PrivateLink",
    "redis": "REDIS",
    "rocketmq": "ROCKETMQ",
    "rocketmq5": "ROCKETMQ",
    "searchengine": "SearchEngine",
    "tsdb": "TSDB",
}

_GLOBAL_SCAN_PACKAGES = {"actiontrail", "cas", "cdn", "cms", "dcdn", "directmail", "dns", "mns", "ram"}

# Cleanup names differ from the public ROS type in these reviewed cases.
_TYPE_OVERRIDES = {
    # DescribeImages rows are individual image resources.  Use the detail route,
    # not the historical CustomImage list route used by the cleanup UI.
    ("ecs", "CustomImage"): "ALIYUN::ECS::Image",
    ("gpdb", "DBInstance"): "ALIYUN::GPDB::DBInstance",
    ("hbr", "DbVault"): "ALIYUN::HBR::Vault",
    ("rocketmq5", "Instance"): "ALIYUN::ROCKETMQ::Instance",
    # The replacement GetLindormInstanceList operation returns Lindorm
    # instances (including ServiceType/EngineType), not legacy TSDB rows.
    # Classifying that response as HiTSDB produced a valid-looking link for
    # the wrong product page.
    ("tsdb", "HiTSDBInstance"): "ALIYUN::Lindorm::Instance",
}

# Public OpenAPI revisions that replaced cleanup-scanner request names or
# response envelopes.  Each replacement was checked against current CLI API
# metadata and a live read-only response shape.
_CONTRACT_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("cas", "DescribeUserCertificateList"): {
        "action": "ListUserCertificateOrder",
        "path": ("CertificateOrderList", "*"),
        "identifierField": "InstanceId",
        "regional": False,
    },
    ("clickhouse", "DescribeDBClusters"): {
        "action": "DescribeDBInstances",
        "path": ("Data", "DBInstances", "*"),
        "identifierField": "DBInstanceId",
    },
    # DescribeContactList rows are identified by Name.  ContactId is not part
    # of the documented response and is not the value rendered by the console.
    ("cms", "DescribeContactList"): {
        "identifierField": "Name",
    },
    ("ess", "DescribeAlarmTask"): {
        "action": "DescribeAlarms",
        "path": ("AlarmList", "Alarm", "*"),
    },
    ("mns", "ListQueue"): {
        "path": ("Data", "PageData", "*"),
        "regional": True,
    },
    ("mns", "ListTopic"): {
        "path": ("Data", "PageData", "*"),
        "regional": True,
    },
    ("nas", "DescribeAccessGroups"): {
        "identifierField": "AccessGroupName",
    },
    ("memcache", "DescribeInstances"): {
        "matchField": "InstanceType",
        "matchValues": ("Memcache",),
    },
    ("redis", "DescribeInstances"): {
        "matchField": "InstanceType",
        "matchValues": ("Redis", "Tair", "tair_rdb", "tair_scm", "tair_essd"),
    },
    # RocketMQ 5.x returns the business rows under data.list.  The cleanup
    # scanner was written against an older SDK model whose generated
    # attribute name made this look like a root-level list.
    ("rocketmq5", "ListInstances"): {
        "path": ("data", "list", "*"),
    },
    ("slb", "DescribeAccessControlLists"): {
        "identifierField": "AclId",
    },
    ("tsdb", "DescribeHiTSDBInstanceList"): {
        "action": "GetLindormInstanceList",
        "path": ("InstanceList", "*"),
    },
}

# Read APIs that expose the same reviewed resource rows as the cleanup scanner,
# but are also used by normal product inventory flows.  Keep these explicit:
# this catalog must never fall back to recursively interpreting arbitrary Id
# fields in an unrelated response.
_ADDITIONAL_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        # Official MaxCompute 2022-01-04 ListProjects: project identity is
        # its own name/regionId, not an opaque ROS physical ID or saleTag.
        "product": "maxcompute", "action": "ListProjects",
        "resourceType": "ALIYUN::MaxCompute::Project",
        "path": ["data", "projects", "*"],
        "identifierField": "name", "regional": True,
    },
    {
        # The official DescribeServerCertificates response and the SLB
        # cleanup scanner identify server certificates by this exact field.
        # CA certificates are a different API/type, never inferred here.
        "product": "slb", "action": "DescribeServerCertificates",
        "resourceType": "alicloud_slb_server_certificate",
        "path": ["ServerCertificates", "ServerCertificate", "*"],
        "identifierField": "ServerCertificateId", "regional": True,
    },
    {
        "product": "slb", "action": "DescribeCACertificates", "resourceType": "alicloud_slb_ca_certificate",
        "path": ["CACertificates", "CACertificate", "*"], "identifierField": "CACertificateId", "regional": True,
    },
    {
        # ListenerPort is the reviewed source component. The runtime joins
        # only the same response/request parent and protocol into the TF ID.
        "product": "slb", "action": "DescribeLoadBalancerAttribute", "resourceType": "alicloud_slb_listener",
        "path": ["ListenerPortsAndProtocol", "ListenerPortAndProtocol", "*"],
        "identifierField": "ListenerPort", "regional": True,
    },
    {
        "product": "slb", "action": "DescribeLoadBalancerListeners", "resourceType": "alicloud_slb_listener",
        "path": ["Listeners", "*"], "identifierField": "ListenerPort", "regional": True,
    },
    {
        "product": "slb", "action": "DescribeMasterSlaveServerGroups",
        "resourceType": "alicloud_slb_master_slave_server_group",
        "path": ["MasterSlaveServerGroups", "MasterSlaveServerGroup", "*"],
        "identifierField": "MasterSlaveServerGroupId", "regional": True,
    },
    {
        "product": "slb", "action": "DescribeRules", "resourceType": "alicloud_slb_rule",
        "path": ["Rules", "Rule", "*"], "identifierField": "RuleId", "regional": True,
    },
    {
        "product": "amqp-open",
        "action": "ListInstances",
        "resourceType": "ALIYUN::AMQP::Instance",
        "path": [
            "Data",
            "Instances",
            "*"
        ],
        "identifierField": "InstanceId",
        "regional": True
    },
    {
        "product": "kms",
        "action": "ListAliases",
        "resourceType": "ALIYUN::KMS::Alias",
        "path": [
            "Aliases",
            "Alias",
            "*"
        ],
        "identifierField": "AliasName",
        "regional": True
    },
    {
        "product": "kms",
        "action": "ListAliases",
        "resourceType": "ALIYUN::KMS::Key",
        "path": [
            "Aliases",
            "Alias",
            "*"
        ],
        "identifierField": "KeyId",
        "regional": True
    },
    {
        "product": "resourcemanager",
        "action": "ListResourceGroups",
        "resourceType": "ALIYUN::ResourceManager::ResourceGroup",
        "path": [
            "ResourceGroups",
            "ResourceGroup",
            "*"
        ],
        "identifierField": "Id",
        "regional": False
    },
    {
        "product": "sae",
        "action": "DescribeNamespaces",
        "resourceType": "ALIYUN::SAE::Namespace",
        "path": [
            "Data",
            "Namespaces",
            "*"
        ],
        "identifierField": "NamespaceName",
        "regional": True
    },
    {
        "product": "sae",
        "action": "ListApplications",
        "resourceType": "ALIYUN::SAE::Application",
        "path": [
            "Data",
            "Applications",
            "*"
        ],
        "identifierField": "AppId",
        "regional": True
    },
    {
        # Reviewed current SAE namespace-list envelope. Terraform owns the
        # NamespaceId identity; do not coerce it into ROS NamespaceName.
        "product": "sae",
        "action": "DescribeNamespaceList",
        "resourceType": "alicloud_sae_namespace",
        "path": ["Data", "*"],
        "identifierField": "NamespaceId",
        "regional": True,
    },
    {
        # The legacy API exposes NamespaceId only on custom rows. Preserve
        # its own ID if present; never turn Default/name-only rows into IDs.
        "product": "sae",
        "action": "DescribeNamespaces",
        "resourceType": "alicloud_sae_namespace",
        "path": ["Data", "Namespaces", "*"],
        "identifierField": "NamespaceId",
        "regional": True,
    },
    {
        # ListClusters returns a flat Data array. InstanceId is the ROS
        # physical/console locator and Terraform id; ClusterId is a different
        # same-row business locator, never a substitute for InstanceId.
        "product": "mse",
        "action": "ListClusters",
        "resourceType": "ALIYUN::MSE::Cluster",
        "path": ["Data", "*"],
        "identifierField": "InstanceId",
        "regional": True,
    },
    {
        # One API response must not emit both ROS and Terraform identities for
        # the same SLS project row: the Web treats two direct types as
        # ambiguous and leaves every project link pending.
        "product": "sls",
        "action": "ListProject",
        "resourceType": "alicloud_log_project",
        "path": ["projects", "*"],
        "identifierField": "projectName",
        "regional": True,
    },
    {
        "product": "ecs",
        "action": "DescribeInstances",
        "resourceType": "alicloud_instance",
        "path": ["Instances", "Instance", "*"],
        "identifierField": "InstanceId",
        "regional": True,
    },
    {
        "product": "vpc",
        "action": "DescribeCommonBandwidthPackages",
        "resourceType": "alicloud_common_bandwidth_package",
        "path": ["CommonBandwidthPackages", "CommonBandwidthPackage", "*"],
        "identifierField": "BandwidthPackageId",
        "regional": True,
    },
    {
        # ACK DescribeClusters returns all cluster editions in one regional
        # response.  Keep the two stable cluster-type identities separate so
        # a normal API response can be matched to the same ROS console route
        # without guessing from a cluster ID prefix.
        "product": "cs",
        "action": "DescribeClusters",
        "resourceType": "ALIYUN::CS::KubernetesCluster",
        "path": ["clusters", "*"],
        "identifierField": "cluster_id",
        "regional": True,
        "matchField": "cluster_type",
        "matchValues": ["Kubernetes"],
    },
    {
        "product": "cs",
        "action": "DescribeClusters",
        "resourceType": "ALIYUN::CS::ManagedKubernetesCluster",
        "path": ["clusters", "*"],
        "identifierField": "cluster_id",
        "regional": True,
        "matchField": "cluster_type",
        "matchValues": ["ManagedKubernetes"],
    },
    {
        # DescribeTrails is the account inventory API.  TrailList rows expose
        # both the console locator (Name) and the stable TrailArn rendered by
        # normal user-facing inventory answers.
        "product": "actiontrail",
        "action": "DescribeTrails",
        "resourceType": "ALIYUN::ACTIONTRAIL::Trail",
        "path": ["TrailList", "*"],
        "identifierField": "Name",
        "regional": False,
    },
    {
        # Enterprise ACR namespaces are scoped by InstanceId.  The current
        # console has no standalone namespace detail page, but the owning
        # instance's namespace list is an exact, verified parent target.
        "product": "cr",
        "action": "ListNamespace",
        "resourceType": "ALIYUN::CR::Namespace",
        "path": ["Namespaces", "*"],
        "identifierField": "NamespaceId",
        "regional": True,
    },
    {
        # Enterprise ACR repositories have a resource-owned detail route that
        # uses the same-row instance, namespace and repository names.
        "product": "cr",
        "action": "ListRepository",
        "resourceType": "ALIYUN::CR::Repository",
        "path": ["Repositories", "*"],
        "identifierField": "RepoId",
        "regional": True,
    },
    {
        # Elasticsearch's ROA inventory returns a flat Result array and uses
        # lower-camel instanceId, unlike most RPC product response fields.
        "product": "elasticsearch",
        "action": "ListInstance",
        "resourceType": "ALIYUN::ElasticSearch::Instance",
        "path": ["Result", "*"],
        "identifierField": "instanceId",
        "regional": True,
    },
    {
        # Listener inventory is a normal ALB read flow.  The runtime already
        # recognizes this exact response shape; keep the generated E2E
        # catalog in sync so a full direct-API run cannot silently skip it.
        "product": "alb",
        "action": "ListListeners",
        "resourceType": "ALIYUN::ALB::Listener",
        "path": ["Listeners", "*"],
        "identifierField": "ListenerId",
        "regional": True,
    },
    {
        # Image caches are independent ECI resources.  The normal inventory
        # response is a flat ImageCaches array, not part of a container-group
        # response.
        "product": "eci",
        "action": "DescribeImageCaches",
        "resourceType": "ALIYUN::ECI::ImageCache",
        "path": ["ImageCaches", "*"],
        "identifierField": "ImageCacheId",
        "regional": True,
    },
    {
        # Access-rule rows carry their stable rule ID and parent access-group
        # name on the same reviewed response object.
        "product": "nas",
        "action": "DescribeAccessRules",
        "resourceType": "ALIYUN::NAS::AccessRule",
        "path": ["AccessRules", "AccessRule", "*"],
        "identifierField": "AccessRuleId",
        "regional": True,
    },
    {
        # DescribeFileSystems also returns mount targets nested inside their
        # owning filesystem row. The extractor preserves that exact parent.
        "product": "nas",
        "action": "DescribeFileSystems",
        "resourceType": "ALIYUN::NAS::MountTarget",
        "path": ["FileSystems", "FileSystem", "*", "MountTargets", "MountTarget", "*"],
        "identifierField": "MountTargetDomain",
        "regional": True,
    },
    {
        # The response domain identifies the mount target; its request supplies
        # the owning FileSystemId. Never infer that parent from a domain prefix.
        "product": "nas",
        "action": "DescribeMountTargets",
        "resourceType": "ALIYUN::NAS::MountTarget",
        "path": ["MountTargets", "MountTarget", "*"],
        "identifierField": "MountTargetDomain",
        "regional": True,
    },
    {
        "product": "nas",
        "action": "DescribeDataFlows",
        "resourceType": "ALIYUN::NAS::DataFlow",
        "path": ["DataFlowInfo", "DataFlow", "*"],
        "identifierField": "DataFlowId",
        "regional": True,
    },
    {
        # Current NAS OpenAPI spells the array element key ``Entrie`` and the
        # identifier ``FsetId``.  Map that API spelling to the reviewed
        # ALIYUN::NAS::Fileset identity without prefix inference.
        "product": "nas",
        "action": "DescribeFilesets",
        "resourceType": "ALIYUN::NAS::Fileset",
        "path": ["Entries", "Entrie", "*"],
        "identifierField": "FsetId",
        "regional": True,
    },
    {
        "product": "nas",
        "action": "DescribeProtocolService",
        "resourceType": "ALIYUN::NAS::ProtocolService",
        "path": ["ProtocolServices", "*"],
        "identifierField": "ProtocolServiceId",
        "regional": True,
    },
    {
        "product": "nas",
        "action": "DescribeProtocolMountTarget",
        "resourceType": "ALIYUN::NAS::ProtocolMountTarget",
        "path": ["ProtocolMountTargets", "*"],
        "identifierField": "ProtocolMountTargetDomain",
        "regional": True,
    },
    {
        # ListTable is an OTS data-plane SDK operation.  The runtime adapter
        # normalizes its tuple result into reviewed table rows and preserves
        # the request's InstanceName as the parent console locator.
        "product": "tablestore",
        "action": "ListTable",
        "resourceType": "ALIYUN::OTS::Table",
        "path": ["Tables", "*"],
        "identifierField": "TableName",
        "regional": True,
    },
    {
        # ListVpcInfoByInstance is the management-plane read API for OTS VPC
        # bindings.  VpcList rows use the user-visible InstanceVpcName as the
        # binding locator; the enclosing request supplies the instance scope.
        "product": "tablestore",
        "action": "ListVpcInfoByInstance",
        "resourceType": "ALIYUN::OTS::VpcBinder",
        "path": ["VpcList", "*"],
        "identifierField": "InstanceVpcName",
        "regional": True,
    },
    {
        # DescribeZoneRecords is the normal PrivateZone record inventory API.
        # Each reviewed response row carries both RecordId and its parent
        # ZoneId, which is required by the console's record-list route.
        "product": "pvtz",
        "action": "DescribeZoneRecords",
        "resourceType": "ALIYUN::PVTZ::ZoneRecord",
        "path": ["Records", "Record", "*"],
        "identifierField": "RecordId",
        "regional": True,
    },
    {
        # ListStateConfigurations is the normal OOS inventory operation.  The
        # current response contains a flat StateConfigurations array whose
        # rows carry the exact identifier used by the production console
        # ``inventory/modify`` route.
        "product": "oos",
        "action": "ListStateConfigurations",
        "resourceType": "ALIYUN::OOS::StateConfiguration",
        "path": ["StateConfigurations", "*"],
        "identifierField": "StateConfigurationId",
        "regional": True,
    },
    {
        # The OSS V2 SDK normalizes ListBuckets' XML response to a lower-case
        # ``buckets`` array.  Each row carries the globally unique bucket name
        # plus its actual region, so normal inventory output can produce an
        # exact console link without relying on nearby prose.
        "product": "oss",
        "action": "ListBuckets",
        "resourceType": "ALIYUN::OSS::Bucket",
        "path": ["buckets", "*"],
        "identifierField": "name",
        "regional": True,
    },
    {
        # Accounts are namespaced by the request's DBClusterId. The response
        # contains a flat array, not an Accounts.Account envelope.
        "product": "polardb",
        "action": "DescribeAccounts",
        "resourceType": "ALIYUN::POLARDB::Account",
        "path": ["Accounts", "*"],
        "identifierField": "AccountName",
        "regional": True,
    },
    {
        "product": "polardb", "action": "DescribeDatabases", "resourceType": "alicloud_polardb_database",
        "path": ["Databases", "Database", "*"], "identifierField": "DBName", "regional": True,
    },
    {
        # Official DescribeParameterGroups JSON and the retained raw response
        # use this envelope. Do not reinterpret these templates as instances.
        "product": "rds", "action": "DescribeParameterGroups",
        "resourceType": "alicloud_rds_parameter_group",
        "path": ["ParameterGroups", "ParameterGroup", "*"],
        "identifierField": "ParameterGroupId", "regional": True,
    },
    {
        "product": "polardb", "action": "DescribeDBClusterEndpoints",
        "resourceType": "ALIYUN::POLARDB::DBClusterEndpoint",
        "path": ["Items", "*"], "identifierField": "DBEndpointId", "regional": True,
    },
    {
        "product": "polardb", "action": "DescribeDBClusterEndpoints",
        "resourceType": "ALIYUN::POLARDB::DBClusterEndpointAddress",
        "path": ["Items", "*", "AddressItems", "*"], "identifierField": "ConnectionString", "regional": True,
    },
    {
        # DescribeCens is the account-level inventory API for CEN instances.
        # The public response envelope is Cens.Cen and each row is identified
        # by CenId.
        # The Alibaba Cloud OpenAPI product code used by the real iac-code
        # aliyun_api tool is ``cbn`` (the console/product family is CEN).
        "product": "cbn",
        "action": "DescribeCens",
        "resourceType": "ALIYUN::CEN::CenInstance",
        "path": ["Cens", "Cen", "*"],
        "identifierField": "CenId",
        "regional": False,
    },
    {
        # Bandwidth plans are account-level CEN resources.  This explicit
        # contract intentionally does not interpret other IDs in the row.
        "product": "cbn",
        "action": "DescribeCenBandwidthPackages",
        "resourceType": "ALIYUN::CEN::CenBandwidthPackage",
        "path": ["CenBandwidthPackages", "CenBandwidthPackage", "*"],
        "identifierField": "CenBandwidthPackageId",
        "regional": False,
    },
    {
        # Current CEN OpenAPI responses use flat arrays for transit routers.
        # CenId and RegionId in the same row are retained by the resource
        # identity so the console route can address the exact parent context.
        "product": "cbn",
        "action": "ListTransitRouters",
        "resourceType": "ALIYUN::CEN::TransitRouter",
        "path": ["TransitRouters", "*"],
        "identifierField": "TransitRouterId",
        "regional": True,
    },
    {
        # A VPC attachment detail route needs both the CEN and transit-router
        # parents, all of which are returned on the documented list row.
        "product": "cbn",
        "action": "ListTransitRouterVpcAttachments",
        "resourceType": "ALIYUN::CEN::TransitRouterVpcAttachment",
        "path": ["TransitRouterAttachments", "*"],
        "identifierField": "TransitRouterAttachmentId",
        "regional": True,
    },
    {
        # DescribeImages is handled by the core runtime extractor, but the
        # generated catalog is also the authoritative direct-API E2E
        # inventory. Omitting it made a full catalog run silently skip custom
        # images even though normal Web conversations linked them.
        "product": "ecs",
        "action": "DescribeImages",
        "resourceType": "ALIYUN::ECS::Image",
        "path": ["Images", "Image", "*"],
        "identifierField": "ImageId",
        "regional": True,
    },
    {
        # VPC is another core runtime contract that predates the generated
        # cleanup catalog. Keep it in the reviewed inventory as well so E2E
        # coverage and production extraction describe the same read surface.
        "product": "vpc",
        "action": "DescribeVpcs",
        "resourceType": "ALIYUN::VPC::VPC",
        "path": ["Vpcs", "Vpc", "*"],
        "identifierField": "VpcId",
        "regional": True,
    },
    {
        # VSwitch is extracted by the same core runtime path as VPC.  Keep its
        # normal inventory API in the generated E2E catalog instead of relying
        # on incidental VSwitchIds embedded in another product's response.
        "product": "vpc",
        "action": "DescribeVSwitches",
        "resourceType": "ALIYUN::VPC::VSwitch",
        "path": ["VSwitches", "VSwitch", "*"],
        "identifierField": "VSwitchId",
        "regional": True,
    },
    {
        "product": "vpc",
        "action": "DescribeCustomerGateways",
        "resourceType": "ALIYUN::VPC::CustomerGateway",
        "path": ["CustomerGateways", "CustomerGateway", "*"],
        "identifierField": "CustomerGatewayId",
        "regional": True,
    },
    {
        "product": "vpc",
        "action": "DescribeVpnConnections",
        "resourceType": "ALIYUN::VPC::VpnConnection",
        "path": ["VpnConnections", "VpnConnection", "*"],
        "identifierField": "VpnConnectionId",
        "regional": True,
    },
    {
        "product": "vpc",
        "action": "DescribeRouterInterfaces",
        "resourceType": "ALIYUN::VPC::RouterInterface",
        "path": ["RouterInterfaceSet", "RouterInterfaceType", "*"],
        "identifierField": "RouterInterfaceId",
        "regional": True,
    },
    {
        "product": "vpc",
        "action": "DescribeSnatTableEntries",
        "resourceType": "ALIYUN::VPC::SnatEntry",
        "path": ["SnatTableEntries", "SnatTableEntry", "*"],
        "identifierField": "SnatEntryId",
        "regional": True,
    },
    {
        # Every DescribeContainerGroups row identifies the VPC that owns the
        # container group.  Treat this reviewed response field as a VPC fact
        # in the same credential/turn context so the displayed parent ID does
        # not have to borrow a potentially stale fact from another query.
        "product": "eci",
        "action": "DescribeContainerGroups",
        "resourceType": "ALIYUN::VPC::VPC",
        "path": ["ContainerGroups", "*"],
        "identifierField": "VpcId",
        "regional": True,
    },
    {
        # The same row also authoritatively identifies its vSwitch.  Keep the
        # exact source path and current execution scope rather than inferring
        # the resource from the spelling or prefix of the returned value.
        "product": "eci",
        "action": "DescribeContainerGroups",
        "resourceType": "ALIYUN::VPC::VSwitch",
        "path": ["ContainerGroups", "*"],
        "identifierField": "VSwitchId",
        "regional": True,
    },
    {
        # Current community/basic ClickHouse inventory uses the legacy
        # DescribeDBClusters response alongside the enterprise
        # DescribeDBInstances response retained by the cleanup scanner.
        "product": "clickhouse",
        "action": "DescribeDBClusters",
        "resourceType": "ALIYUN::ClickHouse::DBCluster",
        "path": ["DBClusters", "DBCluster", "*"],
        "identifierField": "DBClusterId",
        "regional": True,
    },
    {
        # EHPC 2024-07-30 returns a flat Clusters array and ClusterId, while
        # the cleanup scanner still describes the older nested envelope.
        "product": "ehpc",
        "action": "ListClusters",
        "resourceType": "ALIYUN::EHPC::Cluster",
        "path": ["Clusters", "*"],
        "identifierField": "ClusterId",
        "regional": True,
    },
    {
        # The current EMR inventory API likewise returns a flat Clusters array
        # with ClusterId rather than the legacy ClusterInfo/Id pair.
        "product": "emr",
        "action": "ListClusters",
        "resourceType": "ALIYUN::EMR::Cluster",
        "path": ["Clusters", "*"],
        "identifierField": "ClusterId",
        "regional": True,
    },
    {
        "product": "cloudapi",
        "action": "DescribeApps",
        "resourceType": "ALIYUN::ApiGateway::App",
        "path": ["Apps", "AppItem", "*"],
        "identifierField": "AppId",
        "regional": True,
    },
    {
        # The public DescribeRouteTables operation is still emitted by current
        # OpenAPI metadata and points to DescribeRouteTableList as a
        # substitution. Normal inventory calls can therefore return either
        # documented response envelope.
        "product": "vpc",
        "action": "DescribeRouteTables",
        "resourceType": "ALIYUN::VPC::RouteTable",
        "path": ["RouteTables", "RouteTable", "*"],
        "identifierField": "RouteTableId",
        "regional": True,
    },
    {
        # RocketMQ 4.x resources are returned by the ONS inventory API; the
        # RocketMQ ListInstances contract covers only 5.x instances.
        "product": "ons",
        "action": "OnsInstanceInServiceList",
        "resourceType": "ALIYUN::ROCKETMQ::Instance",
        "path": ["Data", "InstanceVO", "*"],
        "identifierField": "InstanceId",
        "regional": True,
    },
    {
        # Smart Access Gateway inventory returns the canonical SmartAGId in
        # SmartAccessGateways.SmartAccessGateway rows.
        "product": "smartag",
        "action": "DescribeSmartAccessGateways",
        "resourceType": "ALIYUN::SAG::SmartAccessGateway",
        "path": ["SmartAccessGateways", "SmartAccessGateway", "*"],
        "identifierField": "SmartAGId",
        "regional": True,
    },
    {
        # Cloud Connect Networks have their own reviewed inventory envelope;
        # do not infer CcnId from unrelated smartag responses.
        "product": "smartag",
        "action": "DescribeCloudConnectNetworks",
        "resourceType": "ALIYUN::SAG::CloudConnectNetwork",
        "path": ["CloudConnectNetworks", "CloudConnectNetwork", "*"],
        "identifierField": "CcnId",
        "regional": True,
    },
    {
        # DescribeACLAttribute returns every ACL rule together with the parent
        # AclId needed by the most precise console route.
        "product": "smartag",
        "action": "DescribeACLAttribute",
        "resourceType": "ALIYUN::SAG::ACLRule",
        "path": ["Acrs", "Acr", "*"],
        "identifierField": "AcrId",
        "regional": True,
    },
)


@dataclass(frozen=True)
class Identity:
    resource_type: str
    identifier_field: str
    identifier_kind: str
    scope_kind: str
    product: str
    aliases: tuple[str, ...]
    parent_fields: tuple[str, ...]


# Reviewed identity details that cannot be reconstructed when the optional
# resource-meta checkout is unavailable.  Keep this list narrow: these values
# are taken from the released CHANNEL_LINKS template itself, not from an API
# response guess.
_IDENTITY_OVERRIDES = (
    Identity("ALIYUN::MaxCompute::Project", "name", "name", "regional", "maxcompute", (), ()),
    # Official TF provider SetId(CustomerGatewayId), same regional VPC object.
    Identity("ALIYUN::VPC::CustomerGateway", "CustomerGatewayId", "id", "regional", "vpc",
             ("alicloud_vpn_customer_gateway",), ()),
    Identity("alicloud_slb_server_certificate", "ServerCertificateId", "id", "regional", "slb", (), ()),
    # TF provider SetId(CACertificateId), SetId(MasterSlaveServerGroupId),
    # SetId(RuleId), and SetId(lbId + ':' + protocol + ':' + frontendPort).
    # Keep these TF identities separate from opaque ROS physical locators.
    Identity("alicloud_slb_ca_certificate", "CACertificateId", "id", "regional", "slb", (), ()),
    Identity("alicloud_slb_listener", "ListenerId", "id", "regional", "slb", (), ("load_balancer_id",)),
    Identity("alicloud_slb_master_slave_server_group", "MasterSlaveServerGroupId", "id", "regional", "slb",
             (), ("LoadBalancerId",)),
    Identity("alicloud_slb_rule", "RuleId", "id", "regional", "slb",
             (), ("LoadBalancerId", "ListenerPort", "ListenerProtocol")),
    Identity(
        resource_type="ALIYUN::AMQP::Instance",
        identifier_field="InstanceId",
        identifier_kind="id",
        scope_kind="regional",
        product="amqp-open",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::KMS::Alias",
        identifier_field="AliasName",
        identifier_kind="name",
        scope_kind="regional",
        product="kms",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ResourceManager::ResourceGroup",
        identifier_field="Id",
        identifier_kind="id",
        scope_kind="account",
        product="resourcemanager",
        aliases=("alicloud_resource_manager_resource_group",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::VPC::RouterInterface",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="vpc",
        aliases=("alicloud_router_interface",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::SAE::Application",
        identifier_field="AppId",
        identifier_kind="id",
        scope_kind="regional",
        product="sae",
        aliases=("alicloud_sae_application",),
        parent_fields=(),
    ),
    Identity(
        resource_type="alicloud_sae_namespace",
        identifier_field="NamespaceId",
        identifier_kind="id",
        scope_kind="regional",
        product="sae",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::SAE::Namespace",
        identifier_field="NamespaceName",
        identifier_kind="name",
        scope_kind="regional",
        product="sae",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::SLS::Project",
        identifier_field="projectName",
        identifier_kind="name",
        scope_kind="account",
        product="sls",
        aliases=("alicloud_log_project",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ACTIONTRAIL::Trail",
        identifier_field="Name",
        identifier_kind="name",
        scope_kind="regional",
        product="actiontrail",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::CR::Namespace",
        identifier_field="NamespaceId",
        identifier_kind="id",
        scope_kind="regional",
        product="cr",
        aliases=(),
        parent_fields=("InstanceId",),
    ),
    Identity(
        resource_type="ALIYUN::CR::Repository",
        identifier_field="RepoId",
        identifier_kind="id",
        scope_kind="regional",
        product="cr",
        aliases=(),
        parent_fields=("InstanceId", "RepoNamespaceName"),
    ),
    Identity(
        resource_type="ALIYUN::ElasticSearch::Instance",
        identifier_field="instanceId",
        identifier_kind="id",
        scope_kind="regional",
        product="elasticsearch",
        aliases=(),
        parent_fields=(),
    ),
    # These identities are explicit production rules in resource_link_rules.js
    # rather than CHANNEL_LINKS entries.  Without the overrides below the
    # additional direct-API contracts are filtered out before generation,
    # silently shrinking the live Web acceptance inventory.
    Identity(
        resource_type="ALIYUN::ECS::Image",
        identifier_field="ImageId",
        identifier_kind="id",
        scope_kind="regional",
        product="ecs",
        aliases=("alicloud_image",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::VPC::VPC",
        identifier_field="VpcId",
        identifier_kind="id",
        scope_kind="regional",
        product="vpc",
        aliases=("ALIYUN::ECS::VPC", "alicloud_vpc"),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::VPC::VSwitch",
        identifier_field="VSwitchId",
        identifier_kind="id",
        scope_kind="regional",
        product="vpc",
        aliases=("ALIYUN::ECS::VSwitch", "alicloud_vswitch"),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ROS::StackGroup",
        identifier_field="StackGroupId",
        identifier_kind="id",
        scope_kind="regional",
        product="ros",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ROS::TemplateScratch",
        identifier_field="TemplateScratchId",
        identifier_kind="id",
        scope_kind="regional",
        product="ros",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ALB::BackendServerAttachment",
        identifier_field="ServerGroupId",
        identifier_kind="id",
        scope_kind="regional",
        product="alb",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::ECS::DeploymentSet",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="ecs",
        aliases=("alicloud_ecs_deployment_set",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::CEN::CenInstance",
        identifier_field="CenId",
        identifier_kind="id",
        scope_kind="account",
        product="cbn",
        aliases=("alicloud_cen_instance",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::CEN::CenBandwidthPackage",
        identifier_field="CenBandwidthPackageId",
        identifier_kind="id",
        scope_kind="account",
        product="cbn",
        aliases=("alicloud_cen_bandwidth_package",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::CEN::TransitRouter",
        identifier_field="TransitRouterId",
        identifier_kind="id",
        scope_kind="regional",
        product="cbn",
        aliases=("alicloud_cen_transit_router",),
        parent_fields=("CenId",),
    ),
    Identity(
        resource_type="ALIYUN::CEN::TransitRouterVpcAttachment",
        identifier_field="TransitRouterAttachmentId",
        identifier_kind="id",
        scope_kind="regional",
        product="cbn",
        aliases=("alicloud_cen_transit_router_vpc_attachment",),
        parent_fields=("CenId", "TransitRouterId"),
    ),
    Identity(
        resource_type="ALIYUN::SAG::ACL",
        identifier_field="AclId",
        identifier_kind="id",
        scope_kind="regional",
        product="smartag",
        aliases=("alicloud_sag_acl",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::SAG::ACLRule",
        identifier_field="AcrId",
        identifier_kind="id",
        scope_kind="regional",
        product="smartag",
        aliases=("alicloud_sag_acl_rule",),
        parent_fields=("AclId",),
    ),
    Identity(
        resource_type="ALIYUN::SAG::CloudConnectNetwork",
        identifier_field="CcnId",
        identifier_kind="id",
        scope_kind="regional",
        product="smartag",
        aliases=("alicloud_cloud_connect_network",),
        parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::SAG::SmartAccessGateway",
        identifier_field="SmartAGId",
        identifier_kind="id",
        scope_kind="regional",
        product="smartag",
        aliases=(),
        parent_fields=(),
    ),
    Identity(
        # Both provider identities are the same instance. The historical
        # Terraform Home route omitted RegionId, but live inventory and the
        # resource-owned basic-information route are regional.
        resource_type="ALIYUN::MSE::Cluster",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="mse",
        aliases=("alicloud_mse_cluster",),
        parent_fields=(),
    ),
    Identity(
        # The released ROS/TF routes both consume the exact NatGatewayId
        # projected by DescribeNatGateways, scoped to its request region.
        resource_type="ALIYUN::VPC::NatGateway",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="vpc",
        aliases=("alicloud_nat_gateway",),
        parent_fields=(),
    ),
    Identity(
        # ListServerGroups and the provider import ID identify the same
        # regional NLB group; retain the reviewed ROS/TF detail route.
        resource_type="ALIYUN::NLB::ServerGroup",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="nlb",
        aliases=("alicloud_nlb_server_group",),
        parent_fields=(),
    ),
    Identity(
        # The 4.x ONS provider import ID is the regional InstanceId already
        # extracted by OnsInstanceInServiceList. Preserve the 5.x attributes
        # and response contract while binding its reviewed Terraform alias.
        resource_type="ALIYUN::ROCKETMQ::Instance",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="ons",
        aliases=("alicloud_ons_instance",),
        parent_fields=(),
    ),
    Identity(
        # AccessRuleId is only unique within an access group.  The released
        # CHANNEL_LINKS fallback stops at the group list, so the direct API
        # identity must retain the formal parent before Web can select the
        # verified rule-tab route.
        resource_type="ALIYUN::NAS::AccessRule",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="nas",
        aliases=("alicloud_nas_access_rule",),
        parent_fields=("AccessGroupName", "FileSystemType"),
    ),
    Identity(
        # The Terraform private template and reviewed ListTemplates row use
        # the same regional OOS template-name detail route.
        resource_type="ALIYUN::OOS::Template",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="oos",
        aliases=("alicloud_oos_template",),
        parent_fields=(),
    ),
    Identity(
        # API rows use MountTargetDomain; the provider import ID separately
        # encodes FileSystemId:MountTargetDomain. Both require the exact parent
        # for the released filesystem mount-tab route.
        resource_type="ALIYUN::NAS::MountTarget",
        identifier_field="MountTargetDomain",
        identifier_kind="id",
        scope_kind="regional",
        product="nas",
        aliases=("alicloud_nas_mount_target",),
        parent_fields=("FileSystemId",),
    ),
    Identity(
        # A SNAT entry's console page is namespaced by the owning NAT gateway.
        # Keep this reviewed parent in regenerated inventories even when the
        # optional resource-meta checkout cannot provide it.
        resource_type="ALIYUN::VPC::SnatEntry",
        identifier_field="PhysicalResourceId",
        identifier_kind="id",
        scope_kind="regional",
        product="vpc",
        aliases=(),
        parent_fields=("NatGatewayId",),
    ),
    Identity(
        # DescribeProtocolMountTarget rows omit both parents; the request
        # supplies the CPFS file system and protocol service that namespace
        # the returned domain and ExportId.
        resource_type="ALIYUN::NAS::ProtocolMountTarget",
        identifier_field="ProtocolMountTargetDomain",
        identifier_kind="id",
        scope_kind="regional",
        product="nas",
        aliases=(),
        parent_fields=("FileSystemId", "ProtocolServiceId"),
    ),
    Identity(
        # Official clone/upgrade resources SetId(DBInstanceId); both Read
        # implementations call DescribeDBInstanceAttribute with that same ID.
        resource_type="ALIYUN::RDS::DBInstance", identifier_field="DBInstanceId",
        identifier_kind="id", scope_kind="regional", product="rds",
        aliases=("alicloud_db_instance", "alicloud_rds_clone_db_instance", "alicloud_rds_upgrade_db_instance"),
        parent_fields=(),
    ),
    Identity(
        # The official Terraform resource SetId uses ParameterGroupId. This
        # is independent from ALIYUN::RDS::DBInstanceParameterGroup.
        resource_type="alicloud_rds_parameter_group", identifier_field="ParameterGroupId",
        identifier_kind="id", scope_kind="regional", product="rds",
        aliases=(), parent_fields=(),
    ),
    Identity(
        resource_type="ALIYUN::POLARDB::Account",
        identifier_field="AccountName",
        identifier_kind="name",
        scope_kind="regional",
        product="polardb",
        aliases=("alicloud_polardb_account",),
        parent_fields=("DBClusterId",),
    ),
    Identity(
        resource_type="ALIYUN::POLARDB::DBCluster", identifier_field="PhysicalResourceId",
        identifier_kind="id", scope_kind="regional", product="polardb",
        aliases=("alicloud_polardb_cluster",), parent_fields=(),
    ),
    Identity(
        resource_type="alicloud_polardb_database", identifier_field="DBName",
        identifier_kind="name", scope_kind="regional", product="polardb",
        aliases=(), parent_fields=("DBClusterId",),
    ),
    Identity(
        resource_type="ALIYUN::POLARDB::DBClusterEndpoint", identifier_field="DBEndpointId",
        identifier_kind="id", scope_kind="regional", product="polardb",
        aliases=("alicloud_polardb_endpoint",), parent_fields=("DBClusterId",),
    ),
    Identity(
        resource_type="ALIYUN::POLARDB::DBClusterEndpointAddress", identifier_field="ConnectionString",
        identifier_kind="name", scope_kind="regional", product="polardb",
        aliases=("alicloud_polardb_endpoint_address",), parent_fields=("DBClusterId", "DBEndpointId"),
    ),
    Identity(
        resource_type="ALIYUN::PrivateLink::VpcEndpoint", identifier_field="PhysicalResourceId",
        identifier_kind="id", scope_kind="regional", product="privatelink",
        aliases=("alicloud_privatelink_vpc_endpoint",), parent_fields=(),
    ),
)

# Extra same-row fields needed by verified console routes.  These remain
# separate from the primary identifier: ListUserCertificateOrder identifies
# a certificate order by InstanceId, while the CAS detail route uses the
# returned certificate/order metadata.  ``Upload`` is the API's formal subtype
# discriminator: uploaded certificates use the V2 upload detail route, while
# purchased certificates use the V1 order route.
_IDENTITY_ATTRIBUTE_FIELDS: dict[str, tuple[str, ...]] = {
    "ALIYUN::MaxCompute::Project": ("name", "regionId", "comment", "status", "type"),
    "alicloud_slb_server_certificate": ("ServerCertificateName", "Fingerprint", "CommonName"),
    "alicloud_slb_ca_certificate": ("CACertificateName", "Fingerprint", "CommonName"),
    "alicloud_slb_listener": ("LoadBalancerId", "ListenerPort", "ListenerProtocol", "Description"),
    "alicloud_slb_master_slave_server_group": ("MasterSlaveServerGroupName",),
    "alicloud_slb_rule": ("RuleName", "Domain", "Url", "VServerGroupId"),
    "ALIYUN::POLARDB::DBCluster": ("DBClusterId",),
    "alicloud_polardb_database": ("DBName", "DBClusterId"),
    "ALIYUN::POLARDB::DBClusterEndpoint": ("DBEndpointId", "DBClusterId"),
    "ALIYUN::POLARDB::DBClusterEndpointAddress": ("ConnectionString", "DBClusterId", "DBEndpointId"),
    "ALIYUN::PrivateLink::VpcEndpoint": ("EndpointId",),
    "ALIYUN::AMQP::Instance": ("InstanceId", "InstanceName", "InstanceType", "Status",),
    "ALIYUN::KMS::Alias": ("AliasArn", "AliasName", "KeyId",),
    "ALIYUN::ResourceManager::ResourceGroup": ("DisplayName", "Id", "Name", "Status",),
    "ALIYUN::SAE::Application": ("AppId", "AppName", "AppType", "NamespaceName",),
    "ALIYUN::SAE::Namespace": ("NamespaceName", "RegionId",),
    "alicloud_sae_namespace": ("NamespaceId", "NamespaceName", "NameSpaceShortId", "RegionId"),
    "ALIYUN::SAG::ACLRule": ("Name", "Direction", "Policy", "IpProtocol", "SourceCidr", "DestCidr",
                            "SourcePortRange", "DestPortRange", "Priority"),
    "ALIYUN::ACTIONTRAIL::Trail": ("HomeRegion", "Name", "TrailArn"),
    "ALIYUN::CAS::Certificate": ("CertificateId", "InstanceId", "OrderId", "Upload"),
    "ALIYUN::CR::Namespace": ("InstanceId", "NamespaceId", "NamespaceName"),
    "ALIYUN::CR::Repository": ("InstanceId", "RepoId", "RepoName", "RepoNamespaceName"),
    "ALIYUN::ECI::ImageCache": ("ImageCacheId", "ImageCacheName", "ContainerGroupId"),
    "ALIYUN::NAS::AccessRule": (
        "AccessGroupName",
        "AccessGroupType",
        "AccessRuleId",
        "FileSystemType",
        "SourceCidrIp",
        "Priority",
        "RWAccess",
        "UserAccess",
    ),
    "ALIYUN::OTS::VpcBinder": ("InstanceName", "InstanceVpcName", "RegionNo", "VpcId"),
    "ALIYUN::POLARDB::Account": ("AccountName", "DBClusterId"),
    "ALIYUN::ROCKETMQ::Instance": ("regionId", "seriesCode", "serviceCode"),
    "ALIYUN::ROS::StackGroup": ("StackGroupName",),
    "ALIYUN::VPC::RouterInterface": ("RouterId", "RouterType"),
    "ALIYUN::VPC::SnatEntry": ("NatGatewayId",),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalog_types(path: Path) -> tuple[set[str], dict[str, str], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    templates = {
        resource_type: template
        for template, resource_types in data["official"]["groups"]
        for resource_type in resource_types
    }
    return set(templates), templates, str(data["official"]["sha256"])


def _primary(attributes: Any) -> str | None:
    if not isinstance(attributes, list):
        return None
    for attribute in attributes:
        if isinstance(attribute, dict) and attribute.get("Primary") and isinstance(attribute.get("ROS"), str):
            return attribute["ROS"]
    return None


def _identities(meta_root: Path, catalog_path: Path) -> tuple[dict[str, Identity], str]:
    catalog_types, templates, catalog_sha = _catalog_types(catalog_path)
    identities: dict[str, Identity] = {
        resource_type: Identity(
            resource_type=resource_type,
            identifier_field="PhysicalResourceId",
            identifier_kind="id",
            scope_kind="regional" if "{RegionId}" in template else "account",
            product="",
            aliases=(),
            parent_fields=tuple(
                sorted(
                    set(re.findall(r"@?\{([A-Za-z0-9_]+)(?:-[^}]*)?\}", template))
                    - {"PhysicalId", "RegionId", "ResourceName"}
                )
            ),
        )
        for resource_type, template in templates.items()
    }
    source_hash = hashlib.sha256()
    for path in sorted((meta_root / "metas").glob("*.yml")):
        source_hash.update(path.name.encode())
        source_hash.update(path.read_bytes())
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("ResourceType"), dict):
            continue
        ros_type = data["ResourceType"].get("ROS")
        terraform_type = data["ResourceType"].get("Terraform")
        matched = tuple(
            item for item in (ros_type, terraform_type) if isinstance(item, str) and item in catalog_types
        )
        if not matched:
            continue
        canonical = ros_type if ros_type in catalog_types else matched[0]
        template = templates[canonical]
        attributes = [
            item.get("ROS")
            for item in data.get("Attributes", [])
            if isinstance(item, dict) and isinstance(item.get("ROS"), str)
        ]
        placeholders = [
            field
            for field in re.findall(r"@?\{([A-Za-z0-9_]+)(?:-[^}]*)?\}", template)
            if field not in {"PhysicalId", "RegionId", "ResourceName"}
        ]
        linked_attributes = list(dict.fromkeys(field for field in placeholders if field in attributes))
        # The console template, rather than a cleanup API's preferred row key,
        # defines the identity shown to the user.  A direct response can expose
        # that identity through another field; response contracts below retain
        # that source field separately.
        identifier_field = (
            linked_attributes[0]
            if len(linked_attributes) == 1 and "{PhysicalId}" not in template
            else "PhysicalResourceId"
        )
        parent_fields = tuple(
            sorted(
                {
                    field
                    for matched_type in matched
                    for field in re.findall(r"@?\{([A-Za-z0-9_]+)(?:-[^}]*)?\}", templates[matched_type])
                }
                - {"PhysicalId", "RegionId", "ResourceName", identifier_field}
            )
        )
        aliases = tuple(item for item in matched if item != canonical)
        for alias in aliases:
            identities.pop(alias, None)
        identities[canonical] = Identity(
            resource_type=canonical,
            identifier_field=identifier_field,
            identifier_kind="name" if _NAME_FIELDS.search(identifier_field) else "id",
            scope_kind="regional" if "{RegionId}" in template else "account",
            product=str(data.get("ProductCode") or ""),
            aliases=aliases,
            parent_fields=parent_fields,
        )
    for item in _IDENTITY_OVERRIDES:
        for alias in item.aliases:
            identities.pop(alias, None)
        identities[item.resource_type] = item
    return identities, source_hash.hexdigest() + ":" + catalog_sha


def _constant_classes(path: Path) -> dict[str, str]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if not isinstance(child, ast.Assign) or not isinstance(child.value, ast.Constant):
                continue
            if not isinstance(child.value.value, str):
                continue
            for target in child.targets:
                if isinstance(target, ast.Name):
                    result[f"{node.name}.{target.id}"] = child.value.value
    return result


def _assigned_resource_type(node: ast.ClassDef, constants: dict[str, str]) -> str | None:
    for child in node.body:
        if not isinstance(child, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "ResourceType" for target in child.targets):
            continue
        if isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
            return child.value.value
        if isinstance(child.value, ast.Attribute) and isinstance(child.value.value, ast.Name):
            return constants.get(f"{child.value.value.id}.{child.value.attr}")
    return None


def _field_from_expression(expression: ast.expr, assignments: dict[str, ast.expr]) -> str | None:
    if isinstance(expression, ast.Name) and expression.id in assignments:
        return _field_from_expression(assignments[expression.id], assignments)
    if isinstance(expression, ast.Subscript) and isinstance(expression.slice, ast.Constant):
        return expression.slice.value if isinstance(expression.slice.value, str) else None
    if isinstance(expression, ast.Call):
        if isinstance(expression.func, ast.Attribute) and expression.func.attr == "get" and expression.args:
            key = expression.args[0]
            return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
        if isinstance(expression.func, ast.Name) and expression.func.id == "str" and expression.args:
            return _field_from_expression(expression.args[0], assignments)
    return None


def _scan_identifier_field(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_resource"
            and node.args
        ):
            if field := _field_from_expression(node.args[0], assignments):
                return field
    return None


def _fetch_contracts(cleanup_root: Path, identities: dict[str, Identity]) -> list[dict[str, Any]]:
    constants = _constant_classes(cleanup_root / "consts.py")
    # Explicit response rules may deliberately use one reviewed Terraform
    # alias to avoid emitting competing identities for the same row.
    catalog_names = set(identities) | {alias for identity in identities.values() for alias in identity.aliases}
    contracts: dict[tuple[str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for path in sorted(cleanup_root.glob("*/*.py")):
        package = path.parent.name
        namespace = _NAMESPACES.get(package, package.upper())
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for class_node in (item for item in module.body if isinstance(item, ast.ClassDef)):
            cleanup_type = _assigned_resource_type(class_node, constants)
            if not cleanup_type:
                continue
            resource_type = _TYPE_OVERRIDES.get((package, cleanup_type), f"ALIYUN::{namespace}::{cleanup_type}")
            if resource_type not in identities:
                continue
            for function in (
                item
                for item in class_node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "handle_scan"
            ):
                response_identifier_field = _scan_identifier_field(function)
                for call in (item for item in ast.walk(function) if isinstance(item, ast.Call)):
                    if not isinstance(call.func, ast.Attribute) or "fetch_all" not in call.func.attr:
                        continue
                    strings = [
                        item.value
                        for item in call.args
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                    request_index = next(
                        (index for index, value in enumerate(strings) if value.endswith("Request")), None
                    )
                    if request_index is None:
                        continue
                    request_name = strings[request_index].rsplit(".", 1)[-1].removesuffix("Request")
                    action_match = _READ_ACTION.search(request_name)
                    if action_match is None:
                        continue
                    action = action_match.group(0)
                    response_path = tuple(strings[request_index + 1 :]) + ("*",)
                    if len(response_path) < 2:
                        continue
                    product = _PRODUCTS.get(package, package)
                    override = _CONTRACT_OVERRIDES.get((package, action), {})
                    action = str(override.get("action") or action)
                    response_path = tuple(override.get("path") or response_path)
                    product = str(override.get("product") or product)
                    identifier_field = (
                        override.get("identifierField")
                        or response_identifier_field
                        or identities[resource_type].identifier_field
                    )
                    if not isinstance(identifier_field, str) or not identifier_field:
                        continue
                    contract = {
                        "product": product,
                        "action": action,
                        "resourceType": resource_type,
                        "path": list(response_path),
                        "identifierField": identifier_field,
                        "regional": bool(override.get("regional", package not in _GLOBAL_SCAN_PACKAGES)),
                    }
                    if match_field := override.get("matchField"):
                        contract["matchField"] = match_field
                        contract["matchValues"] = list(override["matchValues"])
                    contracts[(product, action, resource_type, response_path)] = contract
    for contract in _ADDITIONAL_CONTRACTS:
        resource_type = str(contract["resourceType"])
        if resource_type not in catalog_names:
            continue
        response_path = tuple(str(item) for item in contract["path"])
        contracts[(str(contract["product"]), str(contract["action"]), resource_type, response_path)] = dict(contract)
    return [contracts[key] for key in sorted(contracts)]


def generate(meta_root: Path, extension_root: Path, catalog_path: Path) -> dict[str, Any]:
    identities, identity_hash = _identities(meta_root, catalog_path)
    cleanup_root = extension_root / "extension" / "resource_cleanup"
    contracts = _fetch_contracts(cleanup_root, identities)
    return {
        "schemaVersion": 1,
        "checkedAt": date.today().isoformat(),
        "sources": {
            "resourceMeta": identity_hash,
            "cleanupConsts": _sha256(cleanup_root / "consts.py"),
        },
        "resources": [
            {
                "resourceType": item.resource_type,
                "identifierField": item.identifier_field,
                "identifierKind": item.identifier_kind,
                "scopeKind": item.scope_kind,
                "product": item.product,
                "aliases": list(item.aliases),
                "parentFields": list(item.parent_fields),
                **(
                    {"attributeFields": list(_IDENTITY_ATTRIBUTE_FIELDS[item.resource_type])}
                    if item.resource_type in _IDENTITY_ATTRIBUTE_FIELDS
                    else {}
                ),
            }
            for item in sorted(identities.values(), key=lambda value: value.resource_type)
        ],
        "responseContracts": contracts,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-meta", type=Path, required=True)
    parser.add_argument("--ros-extension", type=Path, required=True)
    parser.add_argument(
        "--link-catalog",
        type=Path,
        default=Path("src/iac_code/web/static/ros_resource_links.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/iac_code/tools/cloud/aliyun/data/direct_resource_api_catalog.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    result = generate(args.resource_meta, args.ros_extension, args.link_catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "resources": len(result["resources"]),
                "responseContracts": len(result["responseContracts"]),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
