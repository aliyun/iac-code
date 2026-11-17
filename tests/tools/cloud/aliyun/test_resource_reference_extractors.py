from __future__ import annotations

import copy
import json

import pytest

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.cloud.aliyun.resource_reference_extractors import (
    credential_reference_context,
    credential_reference_scope_matches,
    direct_api_response_contracts,
    extract_api_resource_references,
)
from iac_code.tools.cloud.resource_references import resource_spec


def extract(product="ram", action="GetRole", params=None, body=None, **kwargs):
    return extract_api_resource_references(
        product=product,
        action=action,
        params=params or {},
        body=body or {},
        region_id="cn-hangzhou",
        tool_use_id="tool-test",
        account_scope="fake-account",
        console_partition="aliyun",
        **kwargs,
    )


def test_nas_rule_keeps_full_visible_permission_tuple():
    row = {
        "AccessRuleId": "1",
        "AccessGroupName": "group-fixture",
        "FileSystemType": "standard",
        "RegionId": "cn-hangzhou",
        "SourceCidrIp": "0.0.0.0/0",
        "Priority": "100",
        "RWAccess": "RDWR",
        "UserAccess": "no_squash",
    }
    result = extract(
        product="nas",
        action="DescribeAccessRules",
        params={"AccessGroupName": "group-fixture", "FileSystemType": "standard"},
        body={"AccessRules": {"AccessRule": [row]}},
    )
    reference = next(item for item in result if item["evidence"] == "api_response")
    assert reference["attributes"]["RWAccess"] == "RDWR"
    assert reference["attributes"]["UserAccess"] == "no_squash"


def test_pvtz_record_keeps_same_response_row_identity_tuple():
    rows = [
        {"RecordId": 123, "ZoneId": "zone-fake", "Rr": "*", "Type": "A", "Value": "192.0.2.1"},
        {"RecordId": 124, "ZoneId": "zone-fake", "Rr": "www", "Type": "A", "Value": "192.0.2.2"},
    ]
    result = extract(product="pvtz", action="DescribeZoneRecords", body={"Records": {"Record": rows}})
    assert len(result) == 2
    assert result[0]["identifier"] == "123"
    assert result[0]["attributes"] == {
        "PhysicalResourceId": "123",
        "RecordId": "123",
        "ZoneId": "zone-fake",
        "Rr": "*",
        "Type": "A",
        "Value": "192.0.2.1",
    }
    assert result[1]["attributes"]["Rr"] == "www"
    assert result[1]["attributes"]["Value"] == "192.0.2.2"


def test_nas_mount_target_retains_exact_request_parent_without_domain_inference():
    body = {"MountTargets": {"MountTarget": [{"MountTargetDomain": "different-prefix.nas.aliyuncs.com"}]}}
    result = extract(
        product="nas", action="DescribeMountTargets", params={"FileSystemId": "fs-formal-parent"}, body=body
    )
    assert len(result) == 1
    assert result[0]["identifier"] == "different-prefix.nas.aliyuncs.com"
    assert result[0]["attributes"]["FileSystemId"] == "fs-formal-parent"
    assert result[0]["sources"][0]["source_path"].endswith("/MountTargetDomain")
    assert resource_spec("alicloud_nas_mount_target") == resource_spec("ALIYUN::NAS::MountTarget")
    without_parent = extract(product="nas", action="DescribeMountTargets", body=body)
    assert "FileSystemId" not in without_parent[0]["attributes"]
    assert "FileSystemId" not in body["MountTargets"]["MountTarget"][0]


def test_nas_nested_mount_targets_keep_each_own_response_parent_and_source():
    body = {
        "FileSystems": {
            "FileSystem": [
                {
                    "FileSystemId": "fs-first",
                    "MountTargets": {
                        "MountTarget": [
                            {"MountTargetDomain": "unrelated-prefix.nas.aliyuncs.com", "Secret": "private"},
                            {"MountTargetDomain": "wrong.nas.aliyuncs.com", "FileSystemId": "fs-second"},
                        ]
                    },
                },
                {
                    "FileSystemId": "fs-second",
                    "MountTargets": {"MountTarget": [{"MountTargetDomain": "second.nas.aliyuncs.com"}]},
                },
                {"MountTargets": {"MountTarget": [{"MountTargetDomain": "orphan.nas.aliyuncs.com"}]}},
            ]
        }
    }
    original = copy.deepcopy(body)
    result = extract(product="nas", action="DescribeFileSystems", params={"FileSystemId": "stale-request"}, body=body)
    mounts = [item for item in result if item["resource_type"] == "ALIYUN::NAS::MountTarget"]
    assert [item["identifier"] for item in mounts] == ["unrelated-prefix.nas.aliyuncs.com", "second.nas.aliyuncs.com"]
    assert [item["attributes"]["FileSystemId"] for item in mounts] == ["fs-first", "fs-second"]
    assert mounts[0]["sources"][0]["source_path"] == (
        "/result/FileSystems/FileSystem/0/MountTargets/MountTarget/0/MountTargetDomain"
    )
    assert mounts[1]["sources"][0]["source_path"].startswith("/result/FileSystems/FileSystem/1/")
    assert body == original
    assert "private" not in json.dumps(result)


def test_polardb_database_and_nested_endpoint_addresses_keep_exact_parents():
    databases = extract(
        product="polardb",
        action="DescribeDatabases",
        params={"DBClusterId": "pc-fixture"},
        body={"Databases": {"Database": [{"DBName": "app"}, {"DBName": "wrong", "DBClusterId": "pc-other"}]}},
    )
    assert len(databases) == 1
    assert databases[0]["resource_type"] == "alicloud_polardb_database"
    assert databases[0]["identifier"] == "app"
    assert databases[0]["attributes"]["DBClusterId"] == "pc-fixture"
    body = {
        "Items": [
            {
                "DBEndpointId": "pe-first",
                "AddressItems": [
                    {"ConnectionString": "first.example.com"},
                    {"ConnectionString": "wrong.example.com", "DBEndpointId": "pe-second"},
                ],
            },
            {"DBEndpointId": "pe-second", "AddressItems": [{"ConnectionString": "second.example.com"}]},
            {"AddressItems": [{"ConnectionString": "orphan.example.com"}]},
        ]
    }
    original = copy.deepcopy(body)
    refs = extract(
        product="polardb", action="DescribeDBClusterEndpoints", params={"DBClusterId": "pc-fixture"}, body=body
    )
    addresses = [ref for ref in refs if ref["resource_type"] == "ALIYUN::POLARDB::DBClusterEndpointAddress"]
    assert [ref["identifier"] for ref in addresses] == ["first.example.com", "second.example.com"]
    assert [ref["attributes"]["DBEndpointId"] for ref in addresses] == ["pe-first", "pe-second"]
    assert all(ref["attributes"]["DBClusterId"] == "pc-fixture" for ref in addresses)
    assert addresses[0]["sources"][0]["source_path"] == "/result/Items/0/AddressItems/0/ConnectionString"
    assert body == original
    assert extract(product="polardb", action="DescribeDBClusterEndpoints", body={"Other": body["Items"]}) == []
    for alias, canonical in [
        ("alicloud_polardb_cluster", "ALIYUN::POLARDB::DBCluster"),
        ("alicloud_polardb_endpoint", "ALIYUN::POLARDB::DBClusterEndpoint"),
        ("alicloud_polardb_endpoint_address", "ALIYUN::POLARDB::DBClusterEndpointAddress"),
        ("alicloud_privatelink_vpc_endpoint", "ALIYUN::PrivateLink::VpcEndpoint"),
    ]:
        assert resource_spec(alias) is resource_spec(canonical)


def test_ram_name_only_and_same_object_id_alias():
    params = {"RoleName": "DeployRole"}
    body = {"Role": {"RoleName": "DeployRole", "RoleId": "123456789012345678", "Description": "private"}}
    original = copy.deepcopy(body)
    result = extract(params=params, body=body)
    assert len(result) == 2
    assert result[0]["evidence"] == "tool_input"
    assert result[0]["aliases"] == []
    assert result[1]["aliases"][0]["identifier"] == "123456789012345678"
    assert all("region_id" not in item for item in result)
    assert result[1]["sources"][0]["source_path"] == "/result/Role/RoleName"
    assert body == original
    assert "private" not in json.dumps(result)


def test_ots_list_table_rows_keep_the_instance_parent_context():
    result = extract(
        product="tablestore",
        action="ListTable",
        params={"InstanceName": "fvt-ros"},
        body={
            "InstanceName": "fvt-ros",
            "Tables": [
                {"TableName": "orders", "InstanceName": "fvt-ros"},
                {"TableName": "inventory", "InstanceName": "fvt-ros"},
            ],
        },
    )

    assert [(item["identifier"], item["attributes"]["InstanceName"]) for item in result] == [
        ("orders", "fvt-ros"),
        ("inventory", "fvt-ros"),
    ]
    assert result[0]["resource_type"] == "ALIYUN::OTS::Table"
    assert result[0]["sources"][0]["source_path"] == "/result/Tables/0/TableName"


def test_ebs_replica_group_keeps_group_name_alias_from_same_api_row():
    result = extract(
        product="ebs",
        action="DescribeDiskReplicaGroups",
        body={
            "ReplicaGroups": [
                {"ReplicaGroupId": "pg-test111111", "GroupName": "test", "Status": "stopped"},
            ]
        },
    )
    assert len(result) == 1
    assert {item["identifier"] for item in result[0]["aliases"]} >= {"pg-test111111", "test"}
    assert {item["identifier_field"] for item in result[0]["aliases"]} >= {"ReplicaGroupId", "GroupName"}


def test_ram_list_keeps_aliases_separate():
    result = extract(
        action="ListRoles",
        body={
            "Roles": {
                "Role": [
                    {"RoleName": "RoleA", "RoleId": "111111111111111111"},
                    {"RoleName": "RoleB", "RoleId": "222222222222222222"},
                ]
            }
        },
    )
    assert [(item["identifier"], item["aliases"][0]["identifier"]) for item in result] == [
        ("RoleA", "111111111111111111"),
        ("RoleB", "222222222222222222"),
    ]
    assert result[1]["sources"][0]["source_path"] == "/result/Roles/Role/1/RoleName"


def test_attachment_input_refers_to_role_not_attachment():
    result = extract(action="AttachPolicyToRole", params={"RoleName": "DeployRole", "PolicyName": "admin"})
    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::RAM::Role"
    assert "admin" not in str(result)


def test_successful_role_deletion_marks_name_reference_deleted():
    result = extract(action="DeleteRole", params={"RoleName": "DeployRole"})
    assert result[0]["status"] == "DELETE_COMPLETE"
    assert result[0]["evidence"] == "tool_input"


def test_bare_role_id_and_unreviewed_action_are_not_name_facts():
    assert extract(params={"RoleId": "123456789012345678"}, body={"RoleId": "123456789012345678"}) == []
    assert extract(action="UnreviewedAction", body={"RoleName": "admin"}) == []
    assert extract(body={"Role": {"DisplayName": "admin"}, "RequestId": "request-1"}) == []


def test_ecs_rows_have_their_own_region_and_related_resource_types():
    result = extract(
        product="ecs",
        action="DescribeInstances",
        body={
            "Instances": {
                "Instance": [
                    {
                        "InstanceId": "i-test111111",
                        "ImageId": "m-image111111",
                        "RegionId": "cn-beijing",
                        "VpcAttributes": {"VpcId": "vpc-test111111"},
                        "SecurityGroupIds": {"SecurityGroupId": ["sg-test111111"]},
                        "NetworkInterfaces": {"NetworkInterface": [{"NetworkInterfaceId": "eni-test111111"}]},
                    },
                    {
                        "InstanceId": "i-test222222",
                        "RegionId": "cn-shanghai",
                        "VpcAttributes": {"VSwitchId": "vsw-test222222"},
                    },
                ]
            },
            "RequestId": "ignored-request",
        },
    )
    by_id = {item["identifier"]: item for item in result}
    assert len(by_id) == 7
    assert by_id["i-test111111"]["region_id"] == "cn-beijing"
    assert by_id["vpc-test111111"]["region_id"] == "cn-beijing"
    assert by_id["sg-test111111"]["resource_type"] == "ALIYUN::ECS::SecurityGroup"
    assert by_id["sg-test111111"]["sources"][0]["source_path"].endswith("/SecurityGroupIds/SecurityGroupId/0")
    assert by_id["vsw-test222222"]["region_id"] == "cn-shanghai"
    assert by_id["vsw-test222222"]["sources"][0]["request_region_id"] == "cn-hangzhou"
    assert by_id["m-image111111"]["resource_type"] == "ALIYUN::ECS::Image"
    assert by_id["eni-test111111"]["resource_type"] == "ALIYUN::ECS::NetworkInterface"


def test_ecs_inventory_actions_extract_primary_and_reviewed_related_resources():
    cases = [
        (
            "DescribeDisks",
            {"Disks": {"Disk": [{"DiskId": "d-disk111111", "InstanceId": "i-instance111111"}]}},
            {"d-disk111111": "ALIYUN::ECS::Disk", "i-instance111111": "ALIYUN::ECS::Instance"},
        ),
        (
            "DescribeSnapshots",
            {"Snapshots": {"Snapshot": [{"SnapshotId": "s-snapshot111111", "SourceDiskId": "d-disk111111"}]}},
            {"s-snapshot111111": "ALIYUN::ECS::Snapshot", "d-disk111111": "ALIYUN::ECS::Disk"},
        ),
        (
            "DescribeImages",
            {
                "Images": {
                    "Image": [
                        {
                            "ImageId": "m-image111111",
                            "DiskDeviceMappings": {"DiskDeviceMapping": [{"SnapshotId": "s-snapshot111111"}]},
                        }
                    ]
                }
            },
            {"m-image111111": "ALIYUN::ECS::Image", "s-snapshot111111": "ALIYUN::ECS::Snapshot"},
        ),
        (
            "DescribeNetworkInterfaces",
            {
                "NetworkInterfaceSets": {
                    "NetworkInterfaceSet": [
                        {
                            "NetworkInterfaceId": "eni-interface111111",
                            "Attachment": {"InstanceId": "i-instance111111"},
                            "VpcId": "vpc-network111111",
                            "VSwitchId": "vsw-network111111",
                            "SecurityGroupIds": {"SecurityGroupId": ["sg-network111111"]},
                        }
                    ]
                }
            },
            {
                "eni-interface111111": "ALIYUN::ECS::NetworkInterface",
                "i-instance111111": "ALIYUN::ECS::Instance",
                "vpc-network111111": "ALIYUN::VPC::VPC",
                "vsw-network111111": "ALIYUN::VPC::VSwitch",
                "sg-network111111": "ALIYUN::ECS::SecurityGroup",
            },
        ),
        (
            "DescribeKeyPairs",
            {"KeyPairs": {"KeyPair": [{"KeyPairName": "deployment-key"}]}},
            {"deployment-key": "ALIYUN::ECS::SSHKeyPair"},
        ),
    ]
    for action, body, expected in cases:
        result = extract(product="ecs", action=action, body=body)
        assert {item["identifier"]: item["resource_type"] for item in result} == expected
        assert all(item["account_scope"] == "fake-account" for item in result)
        assert all(item["console_partition"] == "aliyun" for item in result)
        assert all(item["region_id"] == "cn-hangzhou" for item in result)


@pytest.mark.parametrize("source_disk_id", ["", "none", "NONE", "null", "nil", "n/a", "-"])
def test_ecs_inventory_ignores_empty_or_sentinel_related_ids_and_unreviewed_free_text(source_disk_id):
    result = extract(
        product="ecs",
        action="DescribeSnapshots",
        body={
            "Snapshots": {
                "Snapshot": [
                    {
                        "SnapshotId": "s-snapshot111111",
                        "SourceDiskId": source_disk_id,
                        "Description": "Created_from_i-not-a-reviewed-field",
                    }
                ]
            }
        },
    )
    assert [item["identifier"] for item in result] == ["s-snapshot111111"]


def test_shared_kvstore_api_uses_instance_type_to_separate_redis_and_memcache():
    result = extract(
        product="r-kvstore",
        action="DescribeInstances",
        body={
            "Instances": {
                "KVStoreInstance": [
                    {"InstanceId": "r-redis111111", "InstanceType": "Redis"},
                    {"InstanceId": "r-tair111111", "InstanceType": "Tair"},
                    {"InstanceId": "r-tair-rdb111111", "InstanceType": "tair_rdb"},
                    {"InstanceId": "r-tair-scm111111", "InstanceType": "tair_scm"},
                    {"InstanceId": "r-tair-essd111111", "InstanceType": "tair_essd"},
                    {"InstanceId": "r-memcache111111", "InstanceType": "Memcache"},
                    {"InstanceId": "r-unknown111111"},
                ]
            }
        },
    )
    by_type = {}
    for item in result:
        by_type.setdefault(item["resource_type"], []).append(item["identifier"])
    assert by_type == {
        "ALIYUN::MEMCACHE::Instance": ["r-memcache111111"],
        "ALIYUN::REDIS::Instance": [
            "r-redis111111",
            "r-tair111111",
            "r-tair-rdb111111",
            "r-tair-scm111111",
            "r-tair-essd111111",
        ],
    }


def test_oss_list_buckets_uses_sdk_normalized_name_and_row_region():
    result = extract(
        product="oss",
        action="ListBuckets",
        body={
            "buckets": [
                {
                    "name": "inventory-bucket",
                    "location": "oss-cn-shanghai",
                    "region": "cn-shanghai",
                    "storage_class": "Standard",
                }
            ]
        },
    )

    assert [(item["resource_type"], item["identifier"]) for item in result] == [
        ("ALIYUN::OSS::Bucket", "inventory-bucket")
    ]
    assert result[0]["identifier_field"] == "BucketName"
    assert result[0]["attributes"] == {
        "BucketName": "inventory-bucket",
        "name": "inventory-bucket",
    }
    assert result[0]["region_id"] == "cn-shanghai"
    assert result[0]["sources"][0]["source_path"] == "/result/buckets/0/name"
    assert result[0]["sources"][0]["product"] == "oss"
    assert result[0]["sources"][0]["action"] == "ListBuckets"


def test_clickhouse_community_and_enterprise_inventory_responses_are_both_extracted():
    community = extract(
        product="clickhouse",
        action="DescribeDBClusters",
        body={"DBClusters": {"DBCluster": [{"DBClusterId": "cc-community111111", "Category": "Basic"}]}},
    )
    enterprise = extract(
        product="clickhouse",
        action="DescribeDBInstances",
        body={"Data": {"DBInstances": [{"DBInstanceId": "cc-enterprise111111"}]}},
    )

    assert [(item["resource_type"], item["identifier"]) for item in community] == [
        ("ALIYUN::ClickHouse::DBCluster", "cc-community111111")
    ]
    assert community[0]["sources"][0]["action"] == "DescribeDBClusters"
    assert community[0]["sources"][0]["source_path"].endswith("/DBClusterId")
    assert [(item["resource_type"], item["identifier"]) for item in enterprise] == [
        ("ALIYUN::ClickHouse::DBCluster", "cc-enterprise111111")
    ]
    assert enterprise[0]["sources"][0]["action"] == "DescribeDBInstances"


def test_monitor_group_response_keeps_same_object_name_as_an_alias():
    result = extract(
        product="cms",
        action="DescribeMonitorGroups",
        body={"Resources": {"Resource": [{"GroupId": 61020318, "GroupName": "qitan"}]}},
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::CMS::MonitorGroup"
    assert result[0]["identifier"] == "61020318"
    assert result[0]["identifier_field"] == "PhysicalResourceId"
    assert result[0]["aliases"] == [
        {"identifier": "61020318", "identifier_field": "GroupId", "identifier_kind": "id"},
        {"identifier": "qitan", "identifier_field": "GroupName", "identifier_kind": "name"},
    ]
    assert result[0]["attributes"]["GroupName"] == "qitan"
    assert result[0]["sources"][0]["source_path"] == "/result/Resources/Resource/0/GroupId"


def test_actiontrail_acr_and_elasticsearch_inventory_contracts_keep_exact_locators():
    trail = extract(
        product="actiontrail",
        action="DescribeTrails",
        body={
            "TrailList": [
                {
                    "Name": "audit-trail",
                    "TrailArn": "acs:actiontrail:cn-hangzhou:1234567890123456:trail/audit-trail",
                    "HomeRegion": "cn-hangzhou",
                }
            ]
        },
    )
    namespace = extract(
        product="cr",
        action="ListNamespace",
        body={
            "Namespaces": [
                {
                    "NamespaceId": "crn-test111111",
                    "NamespaceName": "production",
                    "InstanceId": "cri-test111111",
                }
            ]
        },
    )
    repository = extract(
        product="cr",
        action="ListRepository",
        body={
            "Repositories": [
                {
                    "RepoId": "crr-test111111",
                    "RepoName": "backend",
                    "RepoNamespaceName": "production",
                    "InstanceId": "cri-test111111",
                }
            ]
        },
    )
    elasticsearch = extract(
        product="elasticsearch",
        action="ListInstance",
        body={"Result": [{"instanceId": "es-test111111", "description": "search"}]},
    )

    assert trail[0]["identifier"] == "audit-trail"
    assert trail[0]["aliases"] == [
        {
            "identifier": "acs:actiontrail:cn-hangzhou:1234567890123456:trail/audit-trail",
            "identifier_field": "TrailArn",
            "identifier_kind": "id",
        }
    ]
    assert trail[0]["attributes"]["HomeRegion"] == "cn-hangzhou"
    assert namespace[0]["identifier"] == "crn-test111111"
    assert namespace[0]["attributes"] == {
        "InstanceId": "cri-test111111",
        "NamespaceId": "crn-test111111",
        "NamespaceName": "production",
    }
    assert namespace[0]["aliases"][0]["identifier"] == "production"
    assert repository[0]["attributes"]["RepoNamespaceName"] == "production"
    assert repository[0]["aliases"][0]["identifier"] == "backend"
    assert elasticsearch[0]["identifier"] == "es-test111111"


def test_current_and_substitution_vpc_route_table_responses_are_both_extracted():
    current = extract(
        product="vpc",
        action="DescribeRouteTables",
        body={
            "RouteTables": {
                "RouteTable": [
                    {
                        "RouteTableId": "vtb-current111111",
                        "VRouterId": "vrt-current111111",
                        "Status": "Available",
                    }
                ]
            }
        },
    )
    substitution = extract(
        product="vpc",
        action="DescribeRouteTableList",
        body={
            "RouterTableList": {
                "RouterTableListType": [
                    {
                        "RouteTableId": "vtb-substitute111111",
                        "VpcId": "vpc-substitute111111",
                        "VSwitchIds": {"VSwitchId": ["vsw-substitute111111"]},
                    }
                ]
            }
        },
    )

    assert [(item["resource_type"], item["identifier"]) for item in current] == [
        ("ALIYUN::VPC::RouteTable", "vtb-current111111")
    ]
    assert current[0]["sources"][0]["action"] == "DescribeRouteTables"
    assert [(item["resource_type"], item["identifier"]) for item in substitution] == [
        ("ALIYUN::VPC::RouteTable", "vtb-substitute111111"),
        ("ALIYUN::VPC::VPC", "vpc-substitute111111"),
        ("ALIYUN::VPC::VSwitch", "vsw-substitute111111"),
    ]
    assert substitution[1]["sources"][0]["source_path"].endswith("/VpcId")
    assert substitution[2]["sources"][0]["source_path"].endswith("/VSwitchIds/VSwitchId/0")


@pytest.mark.parametrize(
    ("product", "body", "resource_type", "identifier"),
    [
        (
            "ehpc",
            {"Clusters": [{"ClusterId": "ehpc-current111111", "ClusterStatus": "running"}]},
            "ALIYUN::EHPC::Cluster",
            "ehpc-current111111",
        ),
        (
            "emr",
            {"Clusters": [{"ClusterId": "C-CURRENT111111", "ClusterState": "TERMINATED"}]},
            "ALIYUN::EMR::Cluster",
            "C-CURRENT111111",
        ),
    ],
)
def test_current_flat_cluster_inventory_responses_are_extracted(product, body, resource_type, identifier):
    result = extract(product=product, action="ListClusters", body=body)

    assert [(item["resource_type"], item["identifier"]) for item in result] == [(resource_type, identifier)]
    assert result[0]["sources"][0]["source_path"].endswith("/ClusterId")


def test_nat_gateway_reviewed_alias_projects_only_exact_regional_api_row():
    body = {"NatGateways": {"NatGateway": [{"NatGatewayId": "ngw-fake-gateway", "Name": "test"}]}}
    original = copy.deepcopy(body)
    references = extract(product="vpc", action="DescribeNatGateways", params={"RegionId": "cn-shenzhen"}, body=body)
    assert len(references) == 1
    assert references[0]["resource_type"] == "ALIYUN::VPC::NatGateway"
    assert references[0]["identifier"] == "ngw-fake-gateway"
    assert references[0]["region_id"] == "cn-shenzhen"
    assert references[0]["sources"][0]["source_path"] == "/result/NatGateways/NatGateway/0/NatGatewayId"
    assert resource_spec("alicloud_nat_gateway") is resource_spec("ALIYUN::VPC::NatGateway")
    assert resource_spec("alicloud_nat_gateway").scope_kind == "regional"
    assert extract(product="nlb", action="DescribeNatGateways", body=body) == []
    assert extract(product="vpc", action="DescribeNatGateways", body={"Other": body["NatGateways"]}) == []
    assert body == original


def test_nlb_server_group_reviewed_alias_projects_only_exact_regional_api_row():
    body = {"ServerGroups": [{"ServerGroupId": "sgp-fake-group", "ServerGroupName": "test"}]}
    original = copy.deepcopy(body)
    references = extract(product="nlb", action="ListServerGroups", params={"RegionId": "cn-hangzhou"}, body=body)
    assert len(references) == 1
    assert references[0]["resource_type"] == "ALIYUN::NLB::ServerGroup"
    assert references[0]["identifier"] == "sgp-fake-group"
    assert references[0]["region_id"] == "cn-hangzhou"
    assert references[0]["sources"][0]["source_path"] == "/result/ServerGroups/0/ServerGroupId"
    assert resource_spec("alicloud_nlb_server_group") is resource_spec("ALIYUN::NLB::ServerGroup")
    assert resource_spec("alicloud_nlb_server_group").scope_kind == "regional"
    assert extract(product="vpc", action="ListServerGroups", body=body) == []
    assert extract(product="nlb", action="ListServerGroups", body={"Other": body["ServerGroups"]}) == []
    assert body == original


def test_mse_clusters_use_instance_identity_and_same_row_cluster_aliases():
    body = {
        "Data": [
            {
                "InstanceId": "mse-cn-fake-instance",
                "ClusterId": "mse-fake-cluster",
                "ClusterAliasName": "test-cluster",
                "InitStatus": "INIT_SUCCESS",
                "PrivateId": "not-a-resource",
            }
        ]
    }
    original = copy.deepcopy(body)
    result = extract(product="mse", action="ListClusters", params={"RegionId": "cn-shenzhen"}, body=body)
    assert len(result) == 1
    reference = result[0]
    assert reference["resource_type"] == "ALIYUN::MSE::Cluster"
    assert reference["identifier"] == "mse-cn-fake-instance"
    assert reference["region_id"] == "cn-shenzhen"
    assert reference["sources"][0]["source_path"] == "/result/Data/0/InstanceId"
    aliases = {(item["identifier"], item["identifier_kind"]) for item in reference["aliases"]}
    assert ("mse-fake-cluster", "id") in aliases
    assert ("test-cluster", "name") in aliases
    assert "not-a-resource" not in json.dumps(reference)
    assert resource_spec("alicloud_mse_cluster") is resource_spec("ALIYUN::MSE::Cluster")
    assert resource_spec("alicloud_mse_cluster").scope_kind == "regional"
    assert body == original


@pytest.mark.parametrize(
    "body",
    [
        {"Data": [{"ClusterId": "mse-fake-cluster"}]},
        {"Data": {"InstanceId": "mse-cn-fake-instance"}},
        {"Other": [{"InstanceId": "mse-cn-fake-instance"}]},
    ],
)
def test_mse_clusters_require_exact_reviewed_instance_response(body):
    assert extract(product="mse", action="ListClusters", body=body) == []


def test_rocketmq_4x_ons_inventory_response_is_extracted():
    result = extract(
        product="ons",
        action="OnsInstanceInServiceList",
        body={
            "Data": {
                "InstanceVO": [
                    {
                        "InstanceId": "MQ_INST_123456_TEST",
                        "InstanceName": "production",
                        "InstanceStatus": 5,
                    }
                ]
            }
        },
    )

    assert [(item["resource_type"], item["identifier"]) for item in result] == [
        ("ALIYUN::ROCKETMQ::Instance", "MQ_INST_123456_TEST")
    ]
    assert result[0]["sources"][0]["product"] == "ons"
    assert result[0]["sources"][0]["action"] == "OnsInstanceInServiceList"
    assert resource_spec("alicloud_ons_instance") is resource_spec("ALIYUN::ROCKETMQ::Instance")
    assert resource_spec("alicloud_ons_instance").scope_kind == "regional"
    assert set(resource_spec("alicloud_ons_instance").attribute_fields) >= {"regionId", "seriesCode", "serviceCode"}


def test_polardb_accounts_retain_exact_request_cluster_and_terraform_alias():
    body = {"Accounts": [{"AccountName": "fake-account-name", "AccountStatus": "Available"}]}
    original = copy.deepcopy(body)
    refs = extract(product="polardb", action="DescribeAccounts", params={"DBClusterId": "pc-fake-parent"}, body=body)
    assert len(refs) == 1
    assert refs[0]["resource_type"] == "ALIYUN::POLARDB::Account"
    assert refs[0]["identifier"] == "fake-account-name"
    assert refs[0]["identifier_kind"] == "name"
    assert refs[0]["attributes"]["DBClusterId"] == "pc-fake-parent"
    assert refs[0]["sources"][0]["source_path"].endswith("/Accounts/0/AccountName")
    assert resource_spec("alicloud_polardb_account") is resource_spec("ALIYUN::POLARDB::Account")
    assert body == original
    without_parent = extract(product="polardb", action="DescribeAccounts", body=body)
    assert "DBClusterId" not in without_parent[0]["attributes"]
    assert extract(product="polardb", action="DescribeAccounts", body={"Other": body["Accounts"]}) == []


def test_oos_private_template_terraform_alias_uses_exact_reviewed_row():
    body = {"Templates": [{"TemplateName": "fake-private-template", "TemplateId": "t-fake"}]}
    original = copy.deepcopy(body)
    refs = extract(product="oos", action="ListTemplates", params={"RegionId": "cn-hangzhou"}, body=body)
    assert len(refs) == 1
    assert refs[0]["resource_type"] == "ALIYUN::OOS::Template"
    assert refs[0]["identifier"] == "fake-private-template"
    assert refs[0]["attributes"]["TemplateId"] == "t-fake"
    assert resource_spec("alicloud_oos_template") is resource_spec("ALIYUN::OOS::Template")
    assert body == original
    assert extract(product="oos", action="ListTemplates", body={"Other": body["Templates"]}) == []


@pytest.mark.parametrize(
    ("product", "action", "body", "resource_type", "identifier", "attributes", "source_suffix"),
    [
        (
            "eci",
            "DescribeImageCaches",
            {
                "ImageCaches": [
                    {
                        "ImageCacheId": "imc-test123456",
                        "ImageCacheName": "runtime-cache",
                        "ContainerGroupId": "eci-test123456",
                    }
                ]
            },
            "ALIYUN::ECI::ImageCache",
            "imc-test123456",
            {
                "PhysicalResourceId": "imc-test123456",
                "ImageCacheId": "imc-test123456",
                "ImageCacheName": "runtime-cache",
                "ContainerGroupId": "eci-test123456",
            },
            "/ImageCaches/0/ImageCacheId",
        ),
        (
            "nas",
            "DescribeAccessRules",
            {
                "AccessRules": {
                    "AccessRule": [
                        {"AccessRuleId": "42", "AccessGroupName": "DEFAULT_VPC_GROUP_NAME", "RegionId": "cn-beijing"}
                    ]
                }
            },
            "ALIYUN::NAS::AccessRule",
            "42",
            {
                "PhysicalResourceId": "42",
                "AccessRuleId": "42",
                "AccessGroupName": "DEFAULT_VPC_GROUP_NAME",
            },
            "/AccessRules/AccessRule/0/AccessRuleId",
        ),
        (
            "nas",
            "DescribeMountTargets",
            {
                "MountTargets": {
                    "MountTarget": [{"MountTargetDomain": "01234567-cn-hangzhou.nas.aliyuncs.com", "Status": "Active"}]
                }
            },
            "ALIYUN::NAS::MountTarget",
            "01234567-cn-hangzhou.nas.aliyuncs.com",
            {"MountTargetDomain": "01234567-cn-hangzhou.nas.aliyuncs.com"},
            "/MountTargets/MountTarget/0/MountTargetDomain",
        ),
        (
            "nas",
            "DescribeDataFlows",
            {"DataFlowInfo": {"DataFlow": [{"DataFlowId": "df-test123456", "FileSystemId": "cpfs-test123456"}]}},
            "ALIYUN::NAS::DataFlow",
            "df-test123456",
            {"DataFlowId": "df-test123456", "FileSystemId": "cpfs-test123456"},
            "/DataFlowInfo/DataFlow/0/DataFlowId",
        ),
        (
            "nas",
            "DescribeFilesets",
            {"Entries": {"Entrie": [{"FsetId": "fset-test123456", "FileSystemId": "cpfs-test123456"}]}},
            "ALIYUN::NAS::Fileset",
            "fset-test123456",
            {
                "FilesetId": "fset-test123456",
                "FsetId": "fset-test123456",
                "FileSystemId": "cpfs-test123456",
            },
            "/Entries/Entrie/0/FsetId",
        ),
        (
            "nas",
            "DescribeProtocolService",
            {"ProtocolServices": [{"ProtocolServiceId": "ptc-test123456", "FileSystemId": "cpfs-test123456"}]},
            "ALIYUN::NAS::ProtocolService",
            "ptc-test123456",
            {"ProtocolServiceId": "ptc-test123456", "FileSystemId": "cpfs-test123456"},
            "/ProtocolServices/0/ProtocolServiceId",
        ),
        (
            "nas",
            "DescribeProtocolMountTarget",
            {
                "ProtocolMountTargets": [
                    {
                        "ProtocolMountTargetDomain": "ptc-test123456.cpfs.nas.aliyuncs.com",
                        "FileSystemId": "cpfs-test123456",
                    }
                ]
            },
            "ALIYUN::NAS::ProtocolMountTarget",
            "ptc-test123456.cpfs.nas.aliyuncs.com",
            {
                "ProtocolMountTargetDomain": "ptc-test123456.cpfs.nas.aliyuncs.com",
                "FileSystemId": "cpfs-test123456",
            },
            "/ProtocolMountTargets/0/ProtocolMountTargetDomain",
        ),
        (
            "tablestore",
            "ListVpcInfoByInstance",
            {
                "InstanceName": "table-store-instance",
                "VpcList": [
                    {"InstanceVpcName": "application-vpc", "VpcId": "vpc-test123456", "RegionNo": "cn-hangzhou"}
                ],
            },
            "ALIYUN::OTS::VpcBinder",
            "application-vpc",
            {
                "PhysicalResourceId": "application-vpc",
                "InstanceName": "table-store-instance",
                "InstanceVpcName": "application-vpc",
                "RegionNo": "cn-hangzhou",
                "VpcId": "vpc-test123456",
            },
            "/VpcList/0/InstanceVpcName",
        ),
        (
            "rocketmq",
            "ListInstances",
            {
                "data": {
                    "list": [
                        {
                            "instanceId": "rmq-cn-test123456",
                            "instanceName": "production",
                            "regionId": "cn-shenzhen",
                            "seriesCode": "standard",
                        }
                    ]
                }
            },
            "ALIYUN::ROCKETMQ::Instance",
            "rmq-cn-test123456",
            {
                "PhysicalResourceId": "rmq-cn-test123456",
                "instanceId": "rmq-cn-test123456",
                "instanceName": "production",
                "regionId": "cn-shenzhen",
                "seriesCode": "standard",
            },
            "/data/list/0/instanceId",
        ),
    ],
)
def test_live_inventory_response_contracts_are_extracted(
    product, action, body, resource_type, identifier, attributes, source_suffix
):
    result = extract(product=product, action=action, body=body)

    assert [(item["resource_type"], item["identifier"]) for item in result] == [(resource_type, identifier)]
    assert result[0]["attributes"] == attributes
    assert result[0]["sources"][0]["source_path"].endswith(source_suffix)
    assert result[0]["sources"][0]["evidence"] == "api_response"


@pytest.mark.parametrize(
    ("product", "action", "body", "resource_type", "identifier", "attributes", "source_suffix"),
    [
        (
            "amqp-open",
            "ListInstances",
            {
                "Data": {
                    "Instances": [
                        {
                            "InstanceId": "amqp-cn-test123456",
                            "InstanceName": "orders",
                            "InstanceType": "professional",
                            "Status": "SERVING",
                        }
                    ]
                }
            },
            "ALIYUN::AMQP::Instance",
            "amqp-cn-test123456",
            {
                "InstanceId": "amqp-cn-test123456",
                "InstanceName": "orders",
                "InstanceType": "professional",
                "Status": "SERVING",
            },
            "/Data/Instances/0/InstanceId",
        ),
        (
            "kms",
            "ListAliases",
            {
                "Aliases": {
                    "Alias": [
                        {
                            "AliasArn": "acs:kms:cn-hangzhou:1234567890123456:alias/example",
                            "AliasName": "alias/example",
                            "KeyId": "key-test123456",
                        }
                    ]
                }
            },
            "ALIYUN::KMS::Alias",
            "alias/example",
            {
                "AliasArn": "acs:kms:cn-hangzhou:1234567890123456:alias/example",
                "AliasName": "alias/example",
                "KeyId": "key-test123456",
            },
            "/Aliases/Alias/0/AliasName",
        ),
        (
            "resourcemanager",
            "ListResourceGroups",
            {
                "ResourceGroups": {
                    "ResourceGroup": [
                        {
                            "DisplayName": "Production",
                            "Id": "rg-test123456",
                            "Name": "production",
                            "Status": "OK",
                        }
                    ]
                }
            },
            "ALIYUN::ResourceManager::ResourceGroup",
            "rg-test123456",
            {"DisplayName": "Production", "Id": "rg-test123456", "Name": "production", "Status": "OK"},
            "/ResourceGroups/ResourceGroup/0/Id",
        ),
        (
            "sae",
            "ListApplications",
            {
                "Data": {
                    "Applications": [
                        {
                            "AppId": "app-test123456",
                            "AppName": "orders",
                            "AppType": "micro_service",
                            "NamespaceName": "cn-hangzhou:test",
                        }
                    ]
                }
            },
            "ALIYUN::SAE::Application",
            "app-test123456",
            {
                "AppId": "app-test123456",
                "AppName": "orders",
                "AppType": "micro_service",
                "NamespaceName": "cn-hangzhou:test",
            },
            "/Data/Applications/0/AppId",
        ),
        (
            "sae",
            "DescribeNamespaces",
            {
                "Data": {
                    "Namespaces": [
                        {
                            "NamespaceDescription": "test namespace",
                            "NamespaceName": "test",
                            "RegionId": "cn-hangzhou",
                        }
                    ]
                }
            },
            "ALIYUN::SAE::Namespace",
            "test",
            {
                "NamespaceName": "test",
                "RegionId": "cn-hangzhou",
            },
            "/Data/Namespaces/0/NamespaceName",
        ),
    ],
)
def test_direct_inventory_contracts_use_real_response_identifiers(
    product, action, body, resource_type, identifier, attributes, source_suffix
):
    result = extract(product=product, action=action, body=body)
    reference = next(item for item in result if item["resource_type"] == resource_type)

    assert reference["identifier"] == identifier
    assert reference["attributes"] == attributes
    assert reference["sources"][0]["source_path"].endswith(source_suffix)
    assert reference["sources"][0]["product"] == product
    assert reference["sources"][0]["action"] == action
    assert reference["sources"][0]["evidence"] == "api_response"


def test_kms_alias_keeps_arn_alias_and_owning_key_context():
    result = extract(
        product="kms",
        action="ListAliases",
        body={
            "Aliases": {
                "Alias": [
                    {
                        "AliasArn": "acs:kms:cn-hangzhou:1234567890123456:alias/example",
                        "AliasName": "alias/example",
                        "KeyId": "key-test123456",
                    }
                ]
            }
        },
    )
    alias = next(item for item in result if item["resource_type"] == "ALIYUN::KMS::Alias")

    assert alias["aliases"] == [
        {
            "identifier": "acs:kms:cn-hangzhou:1234567890123456:alias/example",
            "identifier_field": "AliasArn",
            "identifier_kind": "id",
        }
    ]
    assert alias["attributes"]["KeyId"] == "key-test123456"


def test_protocol_mount_target_keeps_request_parents_and_export_id_alias():
    result = extract(
        product="nas",
        action="DescribeProtocolMountTarget",
        params={
            "FileSystemId": "cpfs-test123456",
            "ProtocolServiceId": "ptc-test123456",
        },
        body={
            "ProtocolMountTargets": [
                {
                    "ExportId": "exp-test123456",
                    "ProtocolMountTargetDomain": "cpfs-test123456.example.cpfs.aliyuncs.com",
                }
            ]
        },
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::NAS::ProtocolMountTarget"
    assert result[0]["attributes"] == {
        "ProtocolMountTargetDomain": "cpfs-test123456.example.cpfs.aliyuncs.com",
        "FileSystemId": "cpfs-test123456",
        "ProtocolServiceId": "ptc-test123456",
        "ExportId": "exp-test123456",
    }
    assert result[0]["aliases"] == [
        {"identifier": "exp-test123456", "identifier_field": "ExportId", "identifier_kind": "id"}
    ]


@pytest.mark.parametrize(
    ("action", "body", "resource_type", "identifier", "region_id", "attributes", "source_suffix"),
    [
        (
            "DescribeCens",
            {"Cens": {"Cen": [{"CenId": "cen-test111111", "Name": "production"}]}},
            "ALIYUN::CEN::CenInstance",
            "cen-test111111",
            None,
            {"CenId": "cen-test111111"},
            "/CenId",
        ),
        (
            "DescribeCenBandwidthPackages",
            {
                "CenBandwidthPackages": {
                    "CenBandwidthPackage": [{"CenBandwidthPackageId": "cenbwp-test111111", "Status": "InUse"}]
                }
            },
            "ALIYUN::CEN::CenBandwidthPackage",
            "cenbwp-test111111",
            None,
            {"CenBandwidthPackageId": "cenbwp-test111111"},
            "/CenBandwidthPackageId",
        ),
        (
            "ListTransitRouters",
            {
                "TransitRouters": [
                    {
                        "TransitRouterId": "tr-test111111",
                        "CenId": "cen-test111111",
                        "RegionId": "cn-beijing",
                    }
                ]
            },
            "ALIYUN::CEN::TransitRouter",
            "tr-test111111",
            "cn-beijing",
            {"TransitRouterId": "tr-test111111", "CenId": "cen-test111111"},
            "/TransitRouterId",
        ),
        (
            "ListTransitRouterVpcAttachments",
            {
                "TransitRouterAttachments": [
                    {
                        "TransitRouterAttachmentId": "tr-attach-test111111",
                        "TransitRouterId": "tr-test111111",
                        "CenId": "cen-test111111",
                        "VpcRegionId": "cn-shanghai",
                    }
                ]
            },
            "ALIYUN::CEN::TransitRouterVpcAttachment",
            "tr-attach-test111111",
            "cn-shanghai",
            {
                "TransitRouterAttachmentId": "tr-attach-test111111",
                "CenId": "cen-test111111",
                "TransitRouterId": "tr-test111111",
            },
            "/TransitRouterAttachmentId",
        ),
    ],
)
def test_cen_inventory_contracts_keep_exact_identity_and_parent_context(
    action, body, resource_type, identifier, region_id, attributes, source_suffix
):
    result = extract(product="cbn", action=action, body=body)

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier"] == identifier
    assert result[0]["attributes"] == attributes
    if region_id is None:
        assert "region_id" not in result[0]
    else:
        assert result[0]["region_id"] == region_id
    assert result[0]["sources"][0]["source_path"].endswith(source_suffix)
    assert result[0]["sources"][0]["product"] == "cbn"


@pytest.mark.parametrize(
    ("action", "body", "resource_type", "identifier_field", "identifier", "attributes", "source_suffix"),
    [
        (
            "DescribeSmartAccessGateways",
            {"SmartAccessGateways": {"SmartAccessGateway": [{"SmartAGId": "sag-test111111"}]}},
            "ALIYUN::SAG::SmartAccessGateway",
            "SmartAGId",
            "sag-test111111",
            {"SmartAGId": "sag-test111111"},
            "/SmartAGId",
        ),
        (
            "DescribeCloudConnectNetworks",
            {"CloudConnectNetworks": {"CloudConnectNetwork": [{"CcnId": "ccn-test111111"}]}},
            "ALIYUN::SAG::CloudConnectNetwork",
            "CcnId",
            "ccn-test111111",
            {"CcnId": "ccn-test111111"},
            "/CcnId",
        ),
        (
            "DescribeACLs",
            {"Acls": {"Acl": [{"AclId": "acl-test111111"}]}},
            "ALIYUN::SAG::ACL",
            "AclId",
            "acl-test111111",
            {"AclId": "acl-test111111"},
            "/AclId",
        ),
        (
            "DescribeACLAttribute",
            {"Acrs": {"Acr": [{"AcrId": "acr-test111111", "AclId": "acl-test111111"}]}},
            "ALIYUN::SAG::ACLRule",
            "AcrId",
            "acr-test111111",
            {"AcrId": "acr-test111111", "AclId": "acl-test111111"},
            "/AcrId",
        ),
    ],
)
def test_sag_inventory_contracts_keep_exact_identity_and_acl_parent(
    action, body, resource_type, identifier_field, identifier, attributes, source_suffix
):
    result = extract(product="smartag", action=action, body=body)

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier_field"] == identifier_field
    assert result[0]["identifier"] == identifier
    assert result[0]["region_id"] == "cn-hangzhou"
    assert result[0]["attributes"] == attributes
    assert result[0]["evidence"] == "api_response"
    assert result[0]["sources"][0]["source_path"].endswith(source_suffix)
    assert result[0]["sources"][0]["product"] == "smartag"
    assert result[0]["sources"][0]["action"] == action


@pytest.mark.parametrize(
    ("action", "body", "resource_type", "identifier", "name"),
    [
        (
            "DescribeCloudConnectNetworks",
            {"CloudConnectNetworks": {"CloudConnectNetwork": [{"CcnId": "ccn-test111111", "Name": "ros-fvt-pre"}]}},
            "ALIYUN::SAG::CloudConnectNetwork",
            "ccn-test111111",
            "ros-fvt-pre",
        ),
        (
            "DescribeSmartAccessGateways",
            {"SmartAccessGateways": {"SmartAccessGateway": [{"SmartAGId": "sag-test111111", "Name": "ros-test"}]}},
            "ALIYUN::SAG::SmartAccessGateway",
            "sag-test111111",
            "ros-test",
        ),
    ],
)
def test_sag_inventory_keeps_name_alias_from_same_api_row(action, body, resource_type, identifier, name):
    result = extract(product="smartag", action=action, body=body)

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier"] == identifier
    assert {item["identifier"] for item in result[0]["aliases"]} >= {name}


def test_cen_vpc_attachment_extracts_only_reviewed_related_resource_fields():
    result = extract(
        product="cbn",
        action="ListTransitRouterVpcAttachments",
        body={
            "TransitRouterAttachments": [
                {
                    "TransitRouterAttachmentId": "tr-attach-test111111",
                    "TransitRouterId": "tr-test111111",
                    "CenId": "cen-test111111",
                    "VpcId": "vpc-test111111",
                    "VpcRegionId": "cn-shanghai",
                    "ZoneMappings": [
                        {
                            "ZoneId": "cn-shanghai-a",
                            "VSwitchId": "vsw-test111111",
                            "NetworkInterfaceId": "eni-test111111",
                        },
                        {
                            "ZoneId": "cn-shanghai-b",
                            "VSwitchId": "vsw-test222222",
                            "NetworkInterfaceId": "eni-test222222",
                        },
                    ],
                },
                {
                    "TransitRouterAttachmentId": "tr-attach-test222222",
                    "TransitRouterId": "tr-test222222",
                    "CenId": "cen-test222222",
                    "VpcRegionId": "cn-beijing",
                },
            ]
        },
    )

    by_id = {item["identifier"]: item for item in result}
    assert set(by_id) == {
        "tr-attach-test111111",
        "tr-attach-test222222",
        "vpc-test111111",
        "vsw-test111111",
        "vsw-test222222",
        "eni-test111111",
        "eni-test222222",
    }
    assert by_id["vpc-test111111"]["resource_type"] == "ALIYUN::VPC::VPC"
    assert by_id["tr-attach-test111111"]["attributes"]["VpcId"] == "vpc-test111111"
    assert by_id["vsw-test111111"]["resource_type"] == "ALIYUN::VPC::VSwitch"
    assert by_id["eni-test111111"]["resource_type"] == "ALIYUN::ECS::NetworkInterface"
    assert all(item["region_id"] == "cn-shanghai" for key, item in by_id.items() if "111111" in key)
    assert by_id["tr-attach-test222222"]["region_id"] == "cn-beijing"
    assert by_id["vpc-test111111"]["sources"][0]["source_path"].endswith("/TransitRouterAttachments/0/VpcId")
    assert by_id["vsw-test222222"]["sources"][0]["source_path"].endswith(
        "/TransitRouterAttachments/0/ZoneMappings/1/VSwitchId"
    )
    assert by_id["eni-test111111"]["sources"][0]["source_path"].endswith(
        "/TransitRouterAttachments/0/ZoneMappings/0/NetworkInterfaceId"
    )
    assert all(item["evidence"] == "api_response" for item in result)
    assert all(item["sources"][0]["product"] == "cbn" for item in result)
    assert all(item["sources"][0]["action"] == "ListTransitRouterVpcAttachments" for item in result)


@pytest.mark.parametrize(
    "name", [" bad", "bad ", "bad\tgroup", "masked*", "${name}", "https://example.invalid", "bad…"]
)
def test_cms_contact_group_still_rejects_nonliteral_or_incomplete_names(name):
    assert extract(product="cms", action="DescribeContactGroupList", body={
        "ContactGroupList": {"ContactGroup": [{"Name": name}]},
    }) == []


def test_cms_contact_uses_the_documented_name_instead_of_an_undisplayed_numeric_id():
    result = extract(
        product="cms",
        action="DescribeContactList",
        body={
            "Contacts": {
                "Contact": [
                    {"Name": "Alice", "ContactId": 4170763},
                ]
            }
        },
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::CMS::Contact"
    assert result[0]["identifier"] == "Alice"
    assert result[0]["sources"][0]["source_path"].endswith("/Name")


def test_generated_describe_contracts_extract_reviewed_response_paths():
    contracts = direct_api_response_contracts()
    assert len(contracts) >= 80
    assert len({(item["product"], item["action"], item["resource_type"], item["path"]) for item in contracts}) == len(
        contracts
    )
    cases = [
        (
            "sls",
            "ListProject",
            {"projects": [{"projectName": "sls-test-project", "region": "cn-hangzhou"}]},
            "alicloud_log_project",
            "sls-test-project",
        ),
        (
            "ecs",
            "DescribeInstances",
            {"Instances": {"Instance": [{"InstanceId": "i-test111111"}]}},
            "alicloud_instance",
            "i-test111111",
        ),
        (
            "vpc",
            "DescribeCommonBandwidthPackages",
            {"CommonBandwidthPackages": {"CommonBandwidthPackage": [{"BandwidthPackageId": "cbwp-test111111"}]}},
            "alicloud_common_bandwidth_package",
            "cbwp-test111111",
        ),
        (
            "cs",
            "DescribeClusters",
            {"clusters": [{"cluster_id": "c-test111111", "cluster_type": "Kubernetes"}]},
            "ALIYUN::CS::KubernetesCluster",
            "c-test111111",
        ),
        (
            "cs",
            "DescribeClusters",
            {"clusters": [{"cluster_id": "c-test222222", "cluster_type": "ManagedKubernetes"}]},
            "ALIYUN::CS::ManagedKubernetesCluster",
            "c-test222222",
        ),
        (
            "ecs",
            "DescribeDeploymentSets",
            {"DeploymentSets": {"DeploymentSet": [{"DeploymentSetId": "ds-test111111"}]}},
            "ALIYUN::ECS::DeploymentSet",
            "ds-test111111",
        ),
        (
            "ecs",
            "DescribeImages",
            {"Images": {"Image": [{"ImageId": "m-test111111"}]}},
            "ALIYUN::ECS::Image",
            "m-test111111",
        ),
        (
            "alb",
            "ListServerGroups",
            {"ServerGroups": [{"ServerGroupId": "sgp-test111111"}]},
            "ALIYUN::ALB::ServerGroup",
            "sgp-test111111",
        ),
        (
            "cloudapi",
            "DescribeApps",
            {"Apps": {"AppItem": [{"AppId": 111157972, "AppName": "ros_test"}]}},
            "ALIYUN::ApiGateway::App",
            "111157972",
        ),
        (
            "oos",
            "ListExecutions",
            {"Executions": [{"ExecutionId": "exec-test111111"}]},
            "ALIYUN::OOS::Execution",
            "exec-test111111",
        ),
        (
            "ram",
            "ListPolicies",
            {"Policies": {"Policy": [{"PolicyName": "ReadOnlyPolicy"}]}},
            "ALIYUN::RAM::ManagedPolicy",
            "ReadOnlyPolicy",
        ),
        (
            "cms",
            "DescribeMonitorGroups",
            {"Resources": {"Resource": [{"GroupId": 123456, "GroupName": "Production"}]}},
            "ALIYUN::CMS::MonitorGroup",
            "123456",
        ),
        (
            "hitsdb",
            "GetLindormInstanceList",
            {
                "InstanceList": [
                    {
                        "InstanceId": "ld-test111111",
                        "ServiceType": "lindorm",
                        "EngineType": 4,
                    }
                ]
            },
            "ALIYUN::Lindorm::Instance",
            "ld-test111111",
        ),
        (
            "oos",
            "ListPatchBaselines",
            {"PatchBaselines": [{"Id": "pb-test111111", "Name": "Default"}]},
            "ALIYUN::OOS::PatchBaseline",
            "pb-test111111",
        ),
        (
            "oos",
            "ListStateConfigurations",
            {
                "StateConfigurations": [
                    {
                        "StateConfigurationId": "sc-test111111",
                        "TemplateName": "ACS-ECS-InventoryDataCollection",
                    }
                ]
            },
            "ALIYUN::OOS::StateConfiguration",
            "sc-test111111",
        ),
        (
            "vpc",
            "DescribeVpcs",
            {"Vpcs": {"Vpc": [{"VpcId": "vpc-test111111"}]}},
            "ALIYUN::VPC::VPC",
            "vpc-test111111",
        ),
        (
            "vpc",
            "DescribeCustomerGateways",
            {"CustomerGateways": {"CustomerGateway": [{"CustomerGatewayId": "cgw-test111111"}]}},
            "ALIYUN::VPC::CustomerGateway",
            "cgw-test111111",
        ),
        (
            "vpc",
            "DescribeVpnConnections",
            {"VpnConnections": {"VpnConnection": [{"VpnConnectionId": "vco-test111111"}]}},
            "ALIYUN::VPC::VpnConnection",
            "vco-test111111",
        ),
        (
            "vpc",
            "DescribeRouterInterfaces",
            {"RouterInterfaceSet": {"RouterInterfaceType": [{"RouterInterfaceId": "ri-test111111"}]}},
            "ALIYUN::VPC::RouterInterface",
            "ri-test111111",
        ),
        (
            "vpc",
            "DescribeSnatTableEntries",
            {"SnatTableEntries": {"SnatTableEntry": [{"SnatEntryId": "snat-test111111"}]}},
            "ALIYUN::VPC::SnatEntry",
            "snat-test111111",
        ),
    ]
    for product, action, body, resource_type, identifier in cases:
        result = extract(product=product, action=action, body=body)
        assert any(item["resource_type"] == resource_type and item["identifier"] == identifier for item in result)


def test_sls_project_list_has_one_name_based_resource_identity():
    contracts = [
        item for item in direct_api_response_contracts() if item["product"] == "sls" and item["action"] == "ListProject"
    ]
    assert len(contracts) == 1
    assert contracts[0]["resource_type"] == "alicloud_log_project"
    result = extract(
        product="sls",
        action="ListProject",
        body={"projects": [{"projectName": "sls-test-project", "region": "cn-hangzhou"}]},
    )
    assert len(result) == 1
    assert result[0]["resource_type"] == "alicloud_log_project"
    assert result[0]["identifier"] == "sls-test-project"
    assert result[0]["identifier_kind"] == "name"
    assert result[0]["identifier_field"] == "projectName"


def test_generated_describe_contract_accepts_transport_unwrapped_collection():
    result = extract(
        product="cs",
        action="DescribeClusters",
        body=[{"cluster_id": "c-list111111", "cluster_type": "ManagedKubernetes", "name": "test-cluster"}],
    )

    assert any(
        item["resource_type"] == "ALIYUN::CS::ManagedKubernetesCluster" and item["identifier"] == "c-list111111"
        for item in result
    )


def test_ack_describe_clusters_rows_keep_their_own_snake_case_region():
    """One ``cs/DescribeClusters`` answer can span regions.

    The request region only names the endpoint that served the call, so a
    row-owned lower-snake-case ``region_id`` must win.  Without it every
    cluster in a mixed-region response is projected into the caller's region
    and the console route silently opens the wrong region.  Both the reviewed
    wrapper shape and the transport-unwrapped collection must behave the same.
    """
    rows = [
        {
            "cluster_id": "c-test111111",
            "cluster_type": "ManagedKubernetes",
            "name": "prod-hangzhou",
            "region_id": "cn-hangzhou",
        },
        {
            "cluster_id": "c-test222222",
            "cluster_type": "ManagedKubernetes",
            "name": "prod-beijing",
            "region_id": "cn-beijing",
        },
        {
            "cluster_id": "c-test333333",
            "cluster_type": "Kubernetes",
            "name": "legacy-hongkong",
            "region_id": "cn-hongkong",
        },
    ]

    for body in ({"clusters": rows}, rows):
        result = extract(
            product="cs",
            action="DescribeClusters",
            params={"RegionId": "cn-hangzhou"},
            body=body,
        )
        by_id = {item["identifier"]: item for item in result}
        assert by_id["c-test111111"]["resource_type"] == "ALIYUN::CS::ManagedKubernetesCluster"
        assert by_id["c-test222222"]["resource_type"] == "ALIYUN::CS::ManagedKubernetesCluster"
        assert by_id["c-test333333"]["resource_type"] == "ALIYUN::CS::KubernetesCluster"
        assert {key: item["region_id"] for key, item in by_id.items()} == {
            "c-test111111": "cn-hangzhou",
            "c-test222222": "cn-beijing",
            "c-test333333": "cn-hongkong",
        }
        # The request endpoint region stays visible as call context; only the
        # projected resource region comes from the row.
        assert all(item["sources"][0]["request_region_id"] == "cn-hangzhou" for item in result)
        assert all(item["evidence"] == "api_response" for item in result)


def test_ack_describe_clusters_row_without_region_still_uses_request_region():
    """The request endpoint region remains the fallback for rows without one.

    A row that carries no region key, or only a blank one, must keep
    inheriting the request region instead of publishing an unusable region or
    dropping the reference, so only a genuinely row-owned region wins.
    """
    result = extract(
        product="cs",
        action="DescribeClusters",
        params={"RegionId": "cn-shanghai"},
        body=[
            {"cluster_id": "c-test111111", "cluster_type": "ManagedKubernetes", "name": "no-region-row"},
            {
                "cluster_id": "c-test222222",
                "cluster_type": "ManagedKubernetes",
                "name": "beijing-row",
                "region_id": "cn-beijing",
            },
            {
                "cluster_id": "c-test333333",
                "cluster_type": "ManagedKubernetes",
                "name": "blank-region-row",
                "region_id": "   ",
            },
        ],
    )

    assert {item["identifier"]: item["region_id"] for item in result} == {
        "c-test111111": "cn-shanghai",
        "c-test222222": "cn-beijing",
        "c-test333333": "cn-shanghai",
    }
    assert all(item["sources"][0]["request_region_id"] == "cn-shanghai" for item in result)


def test_row_owned_region_precedence_keeps_other_reviewed_spellings():
    """Adding ``region_id`` must not reorder the already-reviewed spellings."""

    def cluster_region(**extra):
        rows = extract(
            product="cs",
            action="DescribeClusters",
            params={"RegionId": "cn-hangzhou"},
            body=[{"cluster_id": "c-precedence1", "cluster_type": "ManagedKubernetes", **extra}],
        )
        return rows[0]["region_id"]

    assert cluster_region(RegionId="cn-shanghai", region_id="cn-beijing") == "cn-shanghai"
    assert cluster_region(regionId="cn-shenzhen", region_id="cn-beijing") == "cn-shenzhen"
    assert cluster_region(region="cn-qingdao", region_id="cn-beijing") == "cn-beijing"
    assert cluster_region(VpcRegionId="cn-chengdu", region_id="cn-beijing") == "cn-beijing"
    # The row region is call evidence, not a locatable attribute or alias.
    single = extract(
        product="cs",
        action="DescribeClusters",
        params={"RegionId": "cn-hangzhou"},
        body=[
            {
                "cluster_id": "c-precedence2",
                "cluster_type": "ManagedKubernetes",
                "name": "prod-beijing",
                "region_id": "cn-beijing",
            }
        ],
    )[0]
    assert "cn-beijing" not in single["attributes"].values()
    assert all(alias["identifier"] != "cn-beijing" for alias in single["aliases"])


def test_vpc_routes_keep_reviewed_subtype_and_parent_context():
    router = extract(
        product="vpc",
        action="DescribeRouterInterfaces",
        body={
            "RouterInterfaceSet": {
                "RouterInterfaceType": [
                    {
                        "RouterInterfaceId": "ri-test111111",
                        "RouterType": "VBR",
                        "RouterId": "vbr-test111111",
                    }
                ]
            }
        },
    )
    assert router[0]["resource_type"] == "ALIYUN::VPC::RouterInterface"
    assert router[0]["attributes"] == {
        "PhysicalResourceId": "ri-test111111",
        "RouterInterfaceId": "ri-test111111",
        "RouterId": "vbr-test111111",
        "RouterType": "VBR",
    }

    snat = extract(
        product="vpc",
        action="DescribeSnatTableEntries",
        body={
            "SnatTableEntries": {
                "SnatTableEntry": [
                    {
                        "SnatEntryId": "snat-test111111",
                        "NatGatewayId": "ngw-test111111",
                        "SnatTableId": "stb-test111111",
                    }
                ]
            }
        },
    )
    assert snat[0]["resource_type"] == "ALIYUN::VPC::SnatEntry"
    assert snat[0]["attributes"] == {
        "PhysicalResourceId": "snat-test111111",
        "SnatEntryId": "snat-test111111",
        "NatGatewayId": "ngw-test111111",
        "SnatTableId": "stb-test111111",
    }


def test_oos_template_name_keeps_same_row_template_id_as_alias():
    result = extract(
        product="oos",
        action="ListTemplates",
        body={
            "Templates": [
                {
                    "TemplateName": "auto-disable-password",
                    "TemplateId": "t-test111111",
                    "ShareType": "Private",
                }
            ]
        },
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::OOS::Template"
    assert result[0]["identifier"] == "auto-disable-password"
    assert result[0]["aliases"] == [
        {"identifier": "t-test111111", "identifier_field": "TemplateId", "identifier_kind": "id"},
        {
            "identifier": "auto-disable-password",
            "identifier_field": "TemplateName",
            "identifier_kind": "name",
        },
    ]
    assert result[0]["attributes"]["TemplateId"] == "t-test111111"
    assert result[0]["sources"][0]["source_path"].endswith("/TemplateName")


def test_oos_patch_baseline_id_keeps_same_row_name_for_console_route():
    result = extract(
        product="oos",
        action="ListPatchBaselines",
        body={
            "PatchBaselines": [
                {
                    "Id": "pb-test111111",
                    "Name": "ACS-Ubuntu-DefaultPatchBaseline",
                    "ShareType": "Public",
                }
            ]
        },
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::OOS::PatchBaseline"
    assert result[0]["identifier"] == "pb-test111111"
    assert result[0]["attributes"]["Name"] == "ACS-Ubuntu-DefaultPatchBaseline"
    assert result[0]["aliases"] == [
        {"identifier": "pb-test111111", "identifier_field": "Id", "identifier_kind": "id"},
        {
            "identifier": "ACS-Ubuntu-DefaultPatchBaseline",
            "identifier_field": "Name",
            "identifier_kind": "name",
        },
    ]
    assert result[0]["sources"][0]["source_path"].endswith("/Id")


@pytest.mark.parametrize(
    ("action", "resource_type", "name"),
    [
        ("ListParameters", "ALIYUN::OOS::Parameter", "folder/normal-parameter"),
        ("ListSecretParameters", "ALIYUN::OOS::SecretParameter", "folder/secret-parameter"),
    ],
)
def test_oos_parameter_id_keeps_same_row_name_for_console_route(action, resource_type, name):
    result = extract(
        product="oos",
        action=action,
        body={"Parameters": [{"Id": "p-test111111", "Name": name}]},
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier"] == "p-test111111"
    assert result[0]["attributes"]["Name"] == name
    assert result[0]["aliases"] == [
        {"identifier": "p-test111111", "identifier_field": "Id", "identifier_kind": "id"},
        {"identifier": name, "identifier_field": "Name", "identifier_kind": "name"},
    ]
    assert result[0]["sources"][0]["source_path"].endswith("/Id")


@pytest.mark.parametrize(
    ("product", "action", "body", "resource_type", "identifier", "attributes"),
    [
        (
            "ram",
            "ListPolicies",
            {"Policies": {"Policy": [{"PolicyName": "AdministratorAccess", "PolicyType": "System"}]}},
            "ALIYUN::RAM::ManagedPolicy",
            "AdministratorAccess",
            {"PhysicalResourceId": "AdministratorAccess", "PolicyName": "AdministratorAccess", "PolicyType": "System"},
        ),
        (
            "ram",
            "ListGroups",
            {"Groups": {"Group": [{"GroupId": "g-test123456", "GroupName": "ops-team"}]}},
            "ALIYUN::RAM::Group",
            "g-test123456",
            {"PhysicalResourceId": "g-test123456", "GroupId": "g-test123456", "GroupName": "ops-team"},
        ),
        (
            "pvtz",
            "DescribeZones",
            {"Zones": {"Zone": [{"ZoneId": "zone-test123456", "ZoneName": "example.internal"}]}},
            "ALIYUN::PVTZ::Zone",
            "zone-test123456",
            {"PhysicalResourceId": "zone-test123456", "ZoneId": "zone-test123456", "ZoneName": "example.internal"},
        ),
        (
            "pvtz",
            "DescribeZoneRecords",
            {"Records": {"Record": [{"RecordId": 202532108001371, "ZoneId": "zone-test123456"}]}},
            "ALIYUN::PVTZ::ZoneRecord",
            "202532108001371",
            {
                "PhysicalResourceId": "202532108001371",
                "RecordId": "202532108001371",
                "ZoneId": "zone-test123456",
            },
        ),
        (
            "tablestore",
            "ListInstances",
            {"Instances": [{"InstanceName": "instance-name"}]},
            "ALIYUN::OTS::Instance",
            "instance-name",
            {"PhysicalResourceId": "instance-name", "InstanceName": "instance-name"},
        ),
        (
            "nas",
            "DescribeAccessGroups",
            {
                "AccessGroups": {
                    "AccessGroup": [
                        {
                            "AccessGroupName": "DEFAULT_CLASSIC_GROUP_NAME",
                            "AccessGroupType": "Classic",
                            "FileSystemType": "standard",
                        }
                    ]
                }
            },
            "ALIYUN::NAS::AccessGroup",
            "DEFAULT_CLASSIC_GROUP_NAME",
            {
                "PhysicalResourceId": "DEFAULT_CLASSIC_GROUP_NAME",
                "AccessGroupName": "DEFAULT_CLASSIC_GROUP_NAME",
                "AccessGroupType": "Classic",
                "FileSystemType": "standard",
            },
        ),
        (
            "cas",
            "ListUserCertificateOrder",
            {
                "CertificateOrderList": [
                    {
                        "InstanceId": "cas-ivauto-test123456",
                        "CertificateId": 17203841,
                        "OrderId": 13268485,
                        "Upload": True,
                    }
                ]
            },
            "ALIYUN::CAS::Certificate",
            "cas-ivauto-test123456",
            {
                "PhysicalResourceId": "cas-ivauto-test123456",
                "CertificateId": "17203841",
                "InstanceId": "cas-ivauto-test123456",
                "OrderId": "13268485",
                "Upload": "true",
            },
        ),
    ],
)
def test_generated_contracts_keep_console_route_attributes(
    product, action, body, resource_type, identifier, attributes
):
    result = extract(product=product, action=action, body=body)
    reference = next(item for item in result if item["resource_type"] == resource_type)
    assert reference["identifier"] == identifier
    assert reference["attributes"] == attributes


@pytest.mark.parametrize(
    "contract",
    direct_api_response_contracts(),
    ids=lambda item: "{}-{}-{}".format(item["product"], item["action"], item["resource_type"]),
)
def test_every_generated_direct_response_contract_reaches_its_identity(contract):
    spec = resource_spec(contract["resource_type"])
    assert spec is not None
    identifier = "fixture-name" if spec.identifier_kind == "name" else "fixture-id-123456"
    body = [{contract["identifier_field"]: identifier}]
    params = {}
    if contract["resource_type"] in {"alicloud_slb_master_slave_server_group", "alicloud_slb_rule"}:
        params = {"LoadBalancerId": "lb-fixture", "ListenerPort": 80, "ListenerProtocol": "http"}
    if contract["resource_type"] == "alicloud_slb_listener":
        body = [{"ListenerPort": 80, "ListenerProtocol": "http"}]
        params = {"LoadBalancerId": "lb-fixture"}
        if contract["action"] == "DescribeLoadBalancerListeners":
            body[0]["LoadBalancerId"] = "lb-fixture"
            params["LoadBalancerId"] = ["lb-fixture"]
        identifier = "lb-fixture:http:80"
    if contract["resource_type"] == "ALIYUN::MaxCompute::Project":
        body[0]["regionId"] = "cn-hangzhou"
        params = {"region": "cn-hangzhou", "maxItem": 10}
    if contract["match_field"]:
        body[0][contract["match_field"]] = contract["match_values"][0]
    for field in reversed(contract["path"][:-1]):
        body = [body] if field == "*" else {field: body}
    if (
        contract["product"] == "nas"
        and contract["action"] == "DescribeFileSystems"
        and (contract["resource_type"] == "ALIYUN::NAS::MountTarget")
    ):
        body["FileSystems"]["FileSystem"][0]["FileSystemId"] = "fixture-parent"
    if contract["resource_type"] == "ALIYUN::POLARDB::DBClusterEndpointAddress":
        body["Items"][0]["DBEndpointId"] = "fixture-endpoint-parent"
    if not contract["path"]:
        body = body[0]
    result = extract(product=contract["product"], action=contract["action"], body=body, params=params)
    assert any(
        item["resource_type"] == contract["resource_type"] and item["identifier"] == identifier for item in result
    )


def test_generated_describe_contract_does_not_scan_outside_reviewed_list_path():
    result = extract(
        product="ecs",
        action="DescribeDeploymentSets",
        body={
            "DeploymentSets": {"DeploymentSet": []},
            "Unrelated": {"DeploymentSetId": "ds-must-not-link"},
        },
    )
    assert result == []


def test_eci_inventory_emits_reviewed_vpc_and_vswitch_facts_in_the_current_context():
    result = extract(
        product="eci",
        action="DescribeContainerGroups",
        params={"RegionId": "cn-shanghai"},
        body={
            "ContainerGroups": [
                {
                    "ContainerGroupId": "eci-test111111",
                    "ContainerGroupName": "normal-inventory-result",
                    "VpcId": "vpc-test111111",
                    "VSwitchId": "vsw-test111111",
                    "Unrelated": {"VpcId": "vpc-must-not-link"},
                }
            ],
            "Unrelated": {"VpcId": "vpc-also-must-not-link"},
        },
    )

    by_id = {item["identifier"]: item for item in result}
    assert set(by_id) == {"eci-test111111", "vpc-test111111", "vsw-test111111"}
    assert by_id["eci-test111111"]["resource_type"] == "ALIYUN::ECI::ContainerGroup"
    assert by_id["vpc-test111111"]["resource_type"] == "ALIYUN::VPC::VPC"
    assert by_id["vsw-test111111"]["resource_type"] == "ALIYUN::VPC::VSwitch"
    assert all(item["region_id"] == "cn-shanghai" for item in result)
    assert all(item["account_scope"] == "fake-account" for item in result)
    assert all(item["console_partition"] == "aliyun" for item in result)
    assert all(item["sources"][0]["product"] == "eci" for item in result)
    assert all(item["sources"][0]["action"] == "DescribeContainerGroups" for item in result)
    assert by_id["vpc-test111111"]["sources"][0]["source_path"].endswith("/ContainerGroups/0/VpcId")
    assert by_id["vsw-test111111"]["sources"][0]["source_path"].endswith("/ContainerGroups/0/VSwitchId")


def test_parent_context_is_not_borrowed_from_a_different_listener():
    result = extract(
        product="alb",
        action="ListListeners",
        params={"ListenerId": "lsn-one", "LoadBalancerId": "alb-one"},
        body={"Listeners": [{"ListenerId": "lsn-two"}]},
    )
    assert result[0]["attributes"]["LoadBalancerId"] == "alb-one"
    assert "LoadBalancerId" not in result[1]["attributes"]


def test_ros_actual_physical_id_survives_api_result():
    result = extract(
        product="ros",
        action="ListStackResources",
        params={"StackId": "stack-1"},
        body={
            "Resources": [
                {
                    "LogicalResourceId": "VM",
                    "ResourceType": "ALIYUN::ECS::Instance",
                    "PhysicalResourceId": "i-test111111",
                },
            ]
        },
    )
    assert result[1]["identifier"] == "i-test111111"
    assert result[1]["logical_resource_id"] == "VM"
    assert result[1]["stack_id"] == "stack-1"


def test_ros_list_stacks_emits_each_stack_as_a_resource_fact():
    result = extract(
        product="ros",
        action="ListStacks",
        body={
            "Stacks": [
                {
                    "StackId": "a67cf255-4dae-49cd-8344-c18d2f2b374a",
                    "StackName": "wait-condition-test",
                    "Status": "CREATE_COMPLETE",
                }
            ]
        },
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == "ALIYUN::ROS::Stack"
    assert result[0]["identifier"] == "a67cf255-4dae-49cd-8344-c18d2f2b374a"
    assert result[0]["identifier_field"] == "StackId"
    assert result[0]["region_id"] == "cn-hangzhou"
    assert result[0]["account_scope"] == "fake-account"
    assert result[0]["console_partition"] == "aliyun"
    assert result[0]["sources"][0]["source_path"] == "/result/Stacks/0/StackId"


@pytest.mark.parametrize(
    ("action", "collection", "identifier_field", "identifier", "resource_type"),
    [
        (
            "ListStackGroups",
            "StackGroups",
            "StackGroupId",
            "29dd2de2-4872-429a-bb82-95930182fa94",
            "ALIYUN::ROS::StackGroup",
        ),
        (
            "ListTemplateScratches",
            "TemplateScratches",
            "TemplateScratchId",
            "ts-15ed16b0b0db460c8806",
            "ALIYUN::ROS::TemplateScratch",
        ),
    ],
)
def test_ros_direct_inventory_uses_the_displayed_response_identifier_field(
    action, collection, identifier_field, identifier, resource_type
):
    row = {identifier_field: identifier}
    if action == "ListStackGroups":
        row["StackGroupName"] = "ros-fvt-pre-cfff5744"
    result = extract(
        product="ros",
        action=action,
        body={collection: [row]},
    )

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier"] == identifier
    assert result[0]["identifier_field"] == identifier_field
    if action == "ListStackGroups":
        assert result[0]["attributes"]["StackGroupName"] == "ros-fvt-pre-cfff5744"
    assert result[0]["sources"][0]["source_path"] == f"/result/{collection}/0/{identifier_field}"


@pytest.mark.parametrize(
    ("product", "action", "body", "resource_type", "response_field", "identifier"),
    [
        (
            "dds",
            "DescribeDBInstances",
            {"DBInstances": {"DBInstance": [{"DBInstanceId": "dds-test111111"}]}},
            "ALIYUN::MONGODB::Instance",
            "DBInstanceId",
            "dds-test111111",
        ),
        (
            "hitsdb",
            "GetLindormInstanceList",
            {"InstanceList": [{"InstanceId": "ld-test111111"}]},
            "ALIYUN::Lindorm::Instance",
            "InstanceId",
            "ld-test111111",
        ),
    ],
)
def test_generic_physical_id_keeps_the_direct_api_response_field_as_an_alias(
    product, action, body, resource_type, response_field, identifier
):
    result = extract(product=product, action=action, body=body)

    assert len(result) == 1
    assert result[0]["resource_type"] == resource_type
    assert result[0]["identifier"] == identifier
    assert result[0]["identifier_field"] == "PhysicalResourceId"
    assert {
        "identifier": identifier,
        "identifier_kind": "id",
        "identifier_field": response_field,
    } in result[0]["aliases"]
    assert result[0]["sources"][0]["source_path"].endswith(f"/{response_field}")


def test_credential_scope_is_opaque_stable_in_process_and_not_based_on_region():
    credential = AliyunCredential(access_key_id="fake-ak", access_key_secret="fake-secret", region_id="cn-hangzhou")
    first = credential_reference_context(credential)
    second = credential_reference_context(
        AliyunCredential(
            access_key_id="fake-ak",
            access_key_secret="fake-secret",
            region_id="cn-beijing",
        )
    )
    assert first == second
    assert first["account_scope"].startswith("credential:")
    assert "fake" not in str(first)
    assert "console_partition" not in first
    assert credential_reference_context(AliyunCredential(access_key_id="other-fake")) != first
    assert credential_reference_context(None) == {}


def test_configured_oauth_scope_survives_rotating_oauth_and_sts_credentials():
    first = AliyunCredential(
        mode="OAuth",
        oauth_site_type="CN",
        oauth_refresh_token="fake-refresh-one",
        access_key_id="fake-sts-ak-one",
        access_key_secret="fake-sts-secret-one",
        sts_token="fake-sts-token-one",
        credential_source="iac_code",
        credential_source_path="/fake/.cloud-credentials.yml",
        oauth_session_id="offline-managed-login",
    )
    rotated = AliyunCredential(
        mode="OAuth",
        oauth_site_type="CN",
        oauth_refresh_token="fake-refresh-two",
        access_key_id="fake-sts-ak-two",
        access_key_secret="fake-sts-secret-two",
        sts_token="fake-sts-token-two",
        credential_source="iac_code",
        credential_source_path="/fake/.cloud-credentials.yml",
        oauth_session_id="offline-managed-login",
    )
    assert credential_reference_context(first) == credential_reference_context(rotated)


def test_explicit_oauth_login_replaces_scope_and_legacy_save_upgrades_generation(tmp_path, monkeypatch):
    from dataclasses import replace

    from iac_code.services.providers import aliyun

    path = tmp_path / "cloud.yml"
    monkeypatch.setattr(aliyun, "get_cloud_credentials_path", lambda: path)
    # Legacy configurations have no login generation and remain readable.
    aliyun._save_yaml(path, {"aliyun": {"mode": "OAuth", "oauth_site_type": "CN",
                                       "oauth_refresh_token": "fake-legacy"}})
    legacy = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert legacy is not None and not legacy.oauth_session_id
    legacy_scope = credential_reference_context(legacy)["account_scope"]
    loaded_legacy = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert credential_reference_scope_matches(loaded_legacy, legacy_scope) is True
    legacy.oauth_refresh_token = "fake-refreshed-legacy"
    aliyun.AliyunCredentials.save(legacy)
    reloaded = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert reloaded is not None and reloaded.oauth_session_id
    assert credential_reference_scope_matches(reloaded, legacy_scope) is False
    upgraded_scope = credential_reference_context(reloaded)["account_scope"]
    reloaded.oauth_refresh_token = "fake-next-refresh"
    aliyun.AliyunCredentials.save(reloaded)
    loaded_upgrade = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert credential_reference_scope_matches(loaded_upgrade, upgraded_scope) is True

    new_login = aliyun.AliyunCredential(mode="OAuth", oauth_site_type="CN", oauth_refresh_token="fake-account-b")
    aliyun.AliyunCredentials.save(new_login)
    loaded = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert loaded is not None and loaded.oauth_session_id == new_login.oauth_session_id
    assert credential_reference_scope_matches(loaded, legacy_scope) is False
    login_scope = credential_reference_context(loaded)["account_scope"]
    refreshed = replace(loaded, oauth_refresh_token="fake-rotated", access_key_id="fake-new-sts")
    aliyun.AliyunCredentials.save(refreshed)
    assert credential_reference_scope_matches(aliyun.AliyunCredentials.load_from_iac_code_config(), login_scope) is True

    # An explicit login to the same file, even in another process, gets a new generation.
    aliyun.AliyunCredentials.save(aliyun.AliyunCredential(mode="OAuth", oauth_site_type="CN",
                                                       oauth_refresh_token="fake-account-c"))
    reloaded = aliyun.AliyunCredentials.load_from_iac_code_config()
    assert credential_reference_scope_matches(reloaded, login_scope) is False


@pytest.mark.parametrize("source,generation", [("iac_code", ""), ("aliyun_cli", ""), ("aliyun_cli", "external-id")])
def test_unmanaged_oauth_profile_replacement_never_reuses_a_path_scope(source, generation):
    from dataclasses import replace

    first = AliyunCredential(
        mode="OAuth", oauth_site_type="CN", oauth_refresh_token="fake-account-a", access_key_id="fake-a",
        credential_source=source, credential_source_path="/fake/profile.json", oauth_session_id=generation,
    )
    scope = credential_reference_context(first)["account_scope"]
    second = replace(first, oauth_refresh_token="fake-account-b", access_key_id="fake-b")
    assert credential_reference_scope_matches(second, scope) is False


def test_request_region_parameter_is_used_when_response_row_omits_region():
    result = extract(
        product="ecs",
        action="DescribeImages",
        params={"RegionId": "cn-shenzhen"},
        body={
            "Images": {
                "Image": [
                    {
                        "ImageId": "m-image111111",
                        "DiskDeviceMappings": {"DiskDeviceMapping": [{"SnapshotId": "s-snapshot111111"}]},
                    }
                ]
            }
        },
    )
    assert {item["identifier"]: item["region_id"] for item in result} == {
        "m-image111111": "cn-shenzhen",
        "s-snapshot111111": "cn-shenzhen",
    }
    assert all(item["sources"][0]["request_region_id"] == "cn-shenzhen" for item in result)


def test_partition_only_comes_from_explicit_site_context():
    assert credential_reference_context(AliyunCredential(oauth_site_type="INTL"))["console_partition"] == "alibabacloud"
    assert credential_reference_context(AliyunCredential(oauth_site_type="CN"))["console_partition"] == "aliyun"


def test_credential_scope_match_distinguishes_changed_from_prior_process_scope():
    first = AliyunCredential(access_key_id="first-fake", access_key_secret="fake")
    second = AliyunCredential(access_key_id="second-fake", access_key_secret="fake")
    scope = credential_reference_context(first)["account_scope"]
    assert credential_reference_scope_matches(first, scope) is True
    assert credential_reference_scope_matches(second, scope) is False
    assert credential_reference_scope_matches(second, "credential:prior-process") is None


def test_optional_fact_collection_failure_does_not_fail_business_result(monkeypatch, caplog):
    from iac_code.tools.cloud.aliyun import resource_reference_extractors as module

    def broken(**_kwargs):
        raise ValueError("fake-secret-must-not-appear-in-diagnostics")

    monkeypatch.setattr(module, "extract_api_resource_references", broken)
    assert module.collect_api_resource_references(credential=None, product="ram", action="GetRole") == []
    assert "Resource reference extraction failed" in caplog.text
    assert "fake-secret" not in caplog.text


def test_nas_access_group_alias_keeps_actual_same_row_name_and_types():
    body = {
        "AccessGroups": {
            "AccessGroup": [
                {"AccessGroupName": "group-fixture", "FileSystemType": "standard", "AccessGroupType": "Vpc"}
            ]
        }
    }
    result = extract(
        product="nas",
        action="DescribeAccessGroups",
        params={"AccessGroupName": "requested-other", "FileSystemType": "extreme", "AccessGroupType": "Classic"},
        body=body,
    )
    reference = next(item for item in result if item["evidence"] == "api_response")
    assert resource_spec("alicloud_nas_access_group") == resource_spec("ALIYUN::NAS::AccessGroup")
    assert reference["identifier"] == "group-fixture"
    assert reference["attributes"]["FileSystemType"] == "standard"
    assert reference["attributes"]["AccessGroupType"] == "Vpc"
    assert reference["sources"][0]["source_path"] == "/result/AccessGroups/AccessGroup/0/AccessGroupName"
    incomplete = extract(
        product="nas",
        action="DescribeAccessGroups",
        params={"FileSystemType": "standard", "AccessGroupType": "Vpc"},
        body={"AccessGroups": {"AccessGroup": [{"AccessGroupName": "group-fixture"}]}},
    )
    observed = next(item for item in incomplete if item["evidence"] == "api_response")
    assert "AccessGroupType" not in observed["attributes"]


@pytest.mark.parametrize("action", ["CreateAccessControlList", "DescribeAccessControlListAttribute"])
def test_slb_acl_root_response_uses_actual_id_and_same_row_details(action):
    body = {
        "AclId": "acl-offline123",
        "AclName": "offline-acl",
        "AddressIPVersion": "ipv4",
        "RequestId": "not-a-resource",
        "Secret": "not-public",
    }
    original = copy.deepcopy(body)
    result = extract(product="slb", action=action, params={"AclName": "request-name"}, body=body)
    references = [item for item in result if item["evidence"] == "api_response"]
    assert len(references) == 1
    reference = references[0]
    assert reference["resource_type"] == "ALIYUN::SLB::AccessControl"
    assert reference["identifier"] == "acl-offline123"
    assert reference["identifier_field"] == "AclId"
    assert reference["region_id"] == "cn-hangzhou"
    assert reference["account_scope"] == "fake-account"
    assert reference["attributes"]["AclName"] == "offline-acl"
    assert reference["attributes"]["AddressIPVersion"] == "ipv4"
    assert reference["sources"][0]["source_path"] == "/result/AclId"
    assert reference["sources"][0]["tool_use_id"] == "tool-test"
    assert "not-public" not in json.dumps(result)
    assert body == original
    assert any(
        item["product"] == "slb"
        and item["action"] == action
        and item["resource_type"] == reference["resource_type"]
        and item["path"] == ()
        for item in direct_api_response_contracts()
    )


@pytest.mark.parametrize(
    "product,action,body",
    [
        ("vpc", "CreateAccessControlList", {"AclId": "acl-offline123"}),
        ("slb", "UnreviewedAccessControlList", {"AclId": "acl-offline123"}),
        ("slb", "CreateAccessControlList", {"AclId": "", "AclName": "offline-acl"}),
        ("slb", "CreateAccessControlList", {"RequestId": "acl-not-a-resource"}),
        ("slb", "DescribeAccessControlListAttribute", {"AclName": "offline-acl"}),
        ("slb", "DescribeAccessControlListAttribute", {"Other": {"AclId": "acl-nested"}}),
    ],
)
def test_slb_acl_root_rejects_unreviewed_sources_or_missing_response_id(product, action, body):
    result = extract(product=product, action=action, params={"AclId": "acl-request-only"}, body=body)
    assert not any(item["evidence"] == "api_response" for item in result)


def test_slb_acl_terraform_stack_response_keeps_its_actual_source_identity():
    result = extract(
        product="ros",
        action="ListStackResources",
        params={"StackId": "stack-offline"},
        body={
            "Resources": [
                {"ResourceType": "alicloud_slb_acl", "PhysicalResourceId": "acl-offline123", "LogicalResourceId": "acl"}
            ]
        },
    )
    references = [item for item in result if item["resource_type"] == "alicloud_slb_acl"]
    assert len(references) == 1
    reference = references[0]
    assert reference["identifier"] == "acl-offline123"
    assert reference["identifier_field"] == "AclId"
    assert reference["stack_id"] == "stack-offline"
    assert reference["logical_resource_id"] == "acl"
    assert reference["sources"][0]["source_path"] == "/result/Resources/0"
    assert reference["sources"][0]["action"] == "ListStackResources"
    assert resource_spec("alicloud_slb_acl") is resource_spec("ALIYUN::SLB::AccessControl")


def test_slb_acl_bare_create_id_never_promotes_request_name_to_response_fields():
    result = extract(
        product="slb",
        action="CreateAccessControlList",
        params={"AclName": "request-only", "AddressIPVersion": "ipv4"},
        body={"AclId": "acl-created123", "RequestId": "request-only"},
    )
    assert len(result) == 1
    assert result[0]["identifier"] == "acl-created123"
    assert result[0]["attributes"] == {"AclId": "acl-created123"}
    assert result[0]["sources"][0]["source_path"] == "/result/AclId"
    assert result[0]["evidence"] == "api_response"


def test_oss_bucket_info_uses_original_sdk_name_and_region_without_mutation():
    body = {
        "bucket_info": {
            "name": "offline-bucket",
            "location": "oss-cn-hangzhou",
            "storage_class": "Standard",
            "creation_date": "2026-10-07T01:02:03+00:00",
            "owner": {"id": "123"},
        }
    }
    before = copy.deepcopy(body)
    result = extract(product="oss", action="GetBucketInfo", params={"bucket": "offline-bucket"}, body=body)
    assert len(result) == 1
    reference = result[0]
    assert reference["resource_type"] == "ALIYUN::OSS::Bucket"
    assert reference["identifier"] == "offline-bucket"
    assert reference["region_id"] == "cn-hangzhou"
    assert reference["account_scope"] == "fake-account"
    assert reference["evidence"] == "api_response"
    assert reference["sources"][0]["source_path"] == "/result/bucket_info/name"
    assert reference["sources"][0]["tool_use_id"] == "tool-test"
    assert body == before


@pytest.mark.parametrize(
    "bucket_info",
    [
        {},
        {"name": True, "location": "oss-cn-hangzhou"},
        {"name": "other-bucket", "location": "oss-cn-hangzhou"},
        {"name": "offline-bucket", "location": "oss-cn-shanghai"},
        {"name": "offline-bucket", "location": "oss-cn-hangzhou", "region_id": "cn-shanghai"},
        {"name": "offline-bucket", "location": "oss-cn-hangzhou", "BucketName": "other-bucket"},
        {"name": "/offline-bucket", "location": "oss-cn-hangzhou"},
        {"name": "offline-bucket"},
    ],
)
def test_oss_bucket_info_does_not_infer_identity_or_wrong_region(bucket_info):
    assert (
        extract(
            product="oss",
            action="GetBucketInfo",
            params={"bucket": "offline-bucket"},
            body={"bucket_info": bucket_info},
        )
        == []
    )


def test_oss_bucket_info_error_and_put_bucket_input_are_not_response_references():
    params = {"bucket": "offline-bucket"}
    assert (
        extract(
            product="oss",
            action="GetBucketInfo",
            params=params,
            body={
                "Code": "NoSuchBucket",
                "BucketName": "offline-bucket",
                "RequestId": "offline-request",
            },
        )
        == []
    )
    assert extract(product="oss", action="PutBucket", params=params) == []


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
def test_ram_policy_root_preserves_only_original_custom_identity(action):
    body = {
        "Policy": {
            "PolicyName": "offline-policy",
            "PolicyType": "Custom",
            "PolicyDocument": "private-policy-document",
            "AccessKeySecret": "private-secret",
        },
        "DefaultPolicyVersion": {"PolicyDocument": "private-version-document"},
        "RequestId": "offline-request",
    }
    params = {"PolicyName": "offline-policy", "PolicyType": "Custom", "PolicyDocument": "private-request"}
    original_body, original_params = copy.deepcopy(body), copy.deepcopy(params)
    references = extract(action=action, params=params, body=body)
    assert len(references) == 1
    reference = references[0]
    assert reference["resource_type"] == "ALIYUN::RAM::ManagedPolicy"
    assert reference["identifier"] == "offline-policy"
    assert reference["identifier_field"] == "PhysicalResourceId"
    assert reference["scope_kind"] == "account"
    assert "region_id" not in reference
    assert reference["attributes"]["PolicyName"] == "offline-policy"
    assert reference["attributes"]["PolicyType"] == "Custom"
    assert reference["evidence"] == "api_response"
    assert reference["sources"][0]["source_path"] == "/result/Policy/PolicyName"
    assert reference["sources"][0]["action"] == action
    assert reference["sources"][0]["tool_use_id"] == "tool-test"
    assert "private-" not in json.dumps(references)
    assert (body, params) == (original_body, original_params)
    assert resource_spec("alicloud_ram_policy") is resource_spec("ALIYUN::RAM::ManagedPolicy")


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
@pytest.mark.parametrize("name", [None, "", " ", True, 123, "name with spaces", "bad/name", "x" * 129])
def test_ram_policy_root_rejects_missing_or_invalid_response_name(action, name):
    assert (
        extract(
            action=action,
            params={"PolicyName": "offline-policy", "PolicyType": "Custom"},
            body={"Policy": {"PolicyName": name, "PolicyType": "Custom"}},
        )
        == []
    )


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
@pytest.mark.parametrize("policy_type", [None, "", "System", "custom", True, 123])
def test_ram_policy_root_requires_actual_custom_type(action, policy_type):
    assert (
        extract(
            action=action,
            params={"PolicyName": "offline-policy", "PolicyType": "Custom"},
            body={"Policy": {"PolicyName": "offline-policy", "PolicyType": policy_type}},
        )
        == []
    )


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
@pytest.mark.parametrize(
    "params,root,row",
    [
        ({"PolicyName": "different-policy"}, {}, {}),
        ({"PolicyName": ""}, {}, {}),
        ({"PolicyType": "System"}, {}, {}),
        ({}, {"PolicyName": "different-policy"}, {}),
        ({}, {"PolicyType": "System"}, {}),
        ({}, {}, {"PhysicalResourceId": "different-policy"}),
        ({"PhysicalResourceId": "request-only"}, {}, {"PolicyName": None}),
    ],
)
def test_ram_policy_root_rejects_conflicting_request_or_response(action, params, root, row):
    body = {**root, "Policy": {"PolicyName": "offline-policy", "PolicyType": "Custom", **row}}
    assert extract(action=action, params=params, body=body) == []


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Policy": {}},
        {"Policy": []},
        {"PolicyName": "offline-policy", "PolicyType": "Custom"},
        {"DefaultPolicyVersion": {"PolicyName": "offline-policy", "PolicyType": "Custom"}},
        {"Policies": {"Policy": [{"PolicyName": "offline-policy", "PolicyType": "Custom"}]}},
    ],
)
def test_ram_policy_root_never_borrows_request_or_unreviewed_nested_identity(action, body):
    assert (
        extract(
            action=action,
            params={"PolicyName": "offline-policy", "PolicyType": "Custom", "PhysicalResourceId": "offline-policy"},
            body=body,
        )
        == []
    )


@pytest.mark.parametrize(
    "product,action",
    [
        ("ecs", "CreatePolicy"),
        ("kms", "GetPolicy"),
        ("ram", "CreatePolicyVersion"),
        ("ram", "GetPolicyVersion"),
        ("ram", "DeletePolicy"),
        ("ram", "UnreviewedPolicyAction"),
    ],
)
def test_ram_policy_root_does_not_expand_unreviewed_action_scope(product, action):
    assert (
        extract(
            product=product,
            action=action,
            params={"PolicyName": "offline-policy", "PolicyType": "Custom"},
            body={"Policy": {"PolicyName": "offline-policy", "PolicyType": "Custom"}},
        )
        == []
    )


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
def test_ram_policy_root_can_use_original_identity_without_request_fallback(action):
    references = extract(action=action, body={"Policy": {"PolicyName": "offline-policy", "PolicyType": "Custom"}})
    assert len(references) == 1
    assert references[0]["sources"][0]["source_path"] == "/result/Policy/PolicyName"


def test_import_key_pair_uses_actual_response_name_without_key_material():
    body = {
        "RequestId": "offline-request",
        "KeyPairName": "offline-returned-key",
        "KeyPairFingerPrint": "offline-fingerprint",
        "PrivateKeyBody": "fake-private-material",
    }
    params = {"KeyPairName": "offline-requested-key", "PublicKeyBody": "fake-public-material"}
    original = copy.deepcopy(body)
    references = extract(product="ecs", action="ImportKeyPair", params=params, body=body)
    responses = [reference for reference in references if reference["evidence"] == "api_response"]
    assert len(responses) == 1
    reference = responses[0]
    assert reference["resource_type"] == "ALIYUN::ECS::SSHKeyPair"
    assert reference["identifier"] == "offline-returned-key"
    assert reference["identifier_field"] == "KeyPairName"
    assert reference["identifier_kind"] == "name"
    assert reference["region_id"] == "cn-hangzhou"
    assert reference["account_scope"] == "fake-account"
    assert reference["sources"][0]["source_path"] == "/result/KeyPairName"
    assert reference["sources"][0]["tool_use_id"] == "tool-test"
    assert body == original
    assert "fake-private-material" not in json.dumps(references)
    assert "fake-public-material" not in json.dumps(references)


@pytest.mark.parametrize("body", [{}, {"RequestId": "offline-request"}, {"KeyPairName": ""}])
def test_import_key_pair_does_not_promote_request_to_created_reference(body):
    references = extract(
        product="ecs", action="ImportKeyPair", params={"KeyPairName": "offline-requested-key"}, body=body
    )
    assert not any(reference["evidence"] == "api_response" for reference in references)


# MaxCompute 2022-01-04 ListProjects: /api/v1/projects, data.projects[*].
# GetProject has a distinct data object; it must not match this list contract.
def maxcompute_project_refs(body, *, product="maxcompute", action="ListProjects", params=None, region="cn-hangzhou"):
    return extract_api_resource_references(
        product=product,
        action=action,
        body=body,
        params={"region": region, "maxItem": 10} if params is None else params,
        region_id=region,
        tool_use_id="offline-project",
        account_scope="fake-account",
        console_partition="aliyun",
    )


def test_maxcompute_project_uses_own_name_region_and_exact_list_source():
    body = {
        "data": {
            "projects": [
                {
                    "name": "offline_first",
                    "regionId": "cn-hangzhou",
                    "comment": "",
                    "status": "AVAILABLE",
                    "type": "managed",
                    "owner": "private-owner",
                    "saleTag": {"resourceId": "not-the-project"},
                },
                {
                    "name": "offline_second",
                    "regionId": "cn-hangzhou",
                    "comment": "second-comment",
                    "status": "READONLY",
                    "type": "external",
                },
            ]
        },
        "requestId": "offline-request",
    }
    before = copy.deepcopy(body)
    refs = maxcompute_project_refs(body)
    assert [ref["identifier"] for ref in refs] == ["offline_first", "offline_second"]
    for index, ref in enumerate(refs):
        assert ref["resource_type"] == "ALIYUN::MaxCompute::Project"
        assert ref["identifier_field"] == "name"
        assert ref["identifier_kind"] == "name"
        assert ref["region_id"] == "cn-hangzhou"
        assert ref["account_scope"] == "fake-account"
        assert ref["console_partition"] == "aliyun"
        assert ref["evidence"] == "api_response"
        assert "PhysicalResourceId" not in ref["attributes"]
        assert ref["sources"][0]["source_path"] == f"/result/data/projects/{index}/name"
        assert ref["sources"][0]["tool_use_id"] == "offline-project"
        assert ref["sources"][0]["request_region_id"] == "cn-hangzhou"
    assert "comment" not in refs[0]["attributes"]
    assert refs[1]["attributes"]["comment"] == "second-comment"
    assert refs[0]["attributes"]["status"] == "AVAILABLE"
    assert "private-owner" not in json.dumps(refs)
    assert "not-the-project" not in json.dumps(refs)
    assert body == before
    (contract,) = [
        item for item in direct_api_response_contracts() if item["resource_type"] == "ALIYUN::MaxCompute::Project"
    ]
    assert (contract["product"], contract["action"], contract["path"], contract["identifier_field"]) == (
        "maxcompute",
        "ListProjects",
        ("data", "projects", "*"),
        "name",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"projects": [{"name": "offline_project", "regionId": "cn-hangzhou"}]},
        {"data": {"name": "offline_project", "regionId": "cn-hangzhou"}},
        {"data": {"projects": {"name": "offline_project", "regionId": "cn-hangzhou"}}},
        {"Data": {"Projects": [{"name": "offline_project", "regionId": "cn-hangzhou"}]}},
        {"data": {"projects": [{"PhysicalResourceId": "offline_project", "regionId": "cn-hangzhou"}]}},
        {"data": {"projects": [{"saleTag": {"resourceId": "offline_project"}, "regionId": "cn-hangzhou"}]}},
    ],
)
def test_maxcompute_project_rejects_unreviewed_shapes_and_borrowed_identity(body):
    assert maxcompute_project_refs(body) == []


@pytest.mark.parametrize(
    "product,action",
    [
        ("mms", "ListProjects"),
        ("sls", "ListProjects"),
        ("maxcompute", "GetProject"),
        ("maxcompute", "CreateProject"),
        ("maxcompute", "DeleteProject"),
    ],
)
def test_maxcompute_project_requires_exact_read_api(product, action):
    body = {"data": {"projects": [{"name": "offline_project", "regionId": "cn-hangzhou"}]}}
    assert maxcompute_project_refs(body, product=product, action=action) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", None),
        ("name", ""),
        ("name", True),
        ("name", 123),
        ("name", " offline_project "),
        ("regionId", None),
        ("regionId", ""),
        ("regionId", True),
        ("regionId", "cn-beijing"),
        ("RegionId", "cn-beijing"),
        ("region", "cn-beijing"),
        ("region_id", "cn-beijing"),
    ],
)
def test_maxcompute_project_rejects_missing_coerced_or_conflicting_row_locators(field, value):
    row = {"name": "offline_project", "regionId": "cn-hangzhou", field: value}
    assert maxcompute_project_refs({"data": {"projects": [row]}}) == []


@pytest.mark.parametrize(
    "params",
    [
        {"region": "cn-beijing"},
        {"region": ""},
        {"region": None},
        {"region": True},
        {"region": "cn-hangzhou", "RegionId": "cn-beijing"},
    ],
)
def test_maxcompute_project_rejects_conflicting_request_region(params):
    body = {"data": {"projects": [{"name": "offline_project", "regionId": "cn-hangzhou"}]}}
    assert maxcompute_project_refs(body, params=params) == []


@pytest.mark.parametrize("error", [{"errorCode": "OBJECT_NOT_EXIST"}, {"httpCode": 404}])
def test_maxcompute_project_does_not_extract_error_envelope(error):
    body = {**error, "data": {"projects": [{"name": "offline_project", "regionId": "cn-hangzhou"}]}}
    assert maxcompute_project_refs(body) == []
