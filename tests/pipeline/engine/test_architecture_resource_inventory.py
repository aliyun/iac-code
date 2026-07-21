from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    AliyunRosResourceTypeClient,
    RosResourceTypeDetail,
    build_resource_inventory_snapshot,
    collect_ros_resource_inventory,
)
from iac_code.tools.base import ToolResult


def _meta_repo() -> ArchitectureMetaRepository:
    return ArchitectureMetaRepository.from_raw(
        categories=[
            {"CategoryCode": "network", "ProductCodes": ["ecs", "vpc"]},
            {"CategoryCode": "database", "ProductCodes": ["polardb"]},
        ],
        products=[
            {"ProductCode": "ecs", "Name": {"en": "ECS", "zh": "云服务器"}, "RelevantCodes": {"ROS": "ECS"}},
            {"ProductCode": "vpc", "Name": {"en": "VPC", "zh": "专有网络"}, "RelevantCodes": {"ROS": "VPC"}},
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
                "ResourceType": {"ROS": "ROS/ALIYUN::POLARDB::DBCluster"},
                "ProductCode": "polardb",
                "Name": {"en": "PolarDB Cluster", "zh": "PolarDB 集群"},
                "Properties": [
                    {
                        "ROS": "VpcId",
                        "Type": "String",
                        "RelatedTo": [{"ResourceType": "ROS/ALIYUN::ECS::VPC"}],
                    }
                ],
            },
            {
                "ResourceType": {"ROS": "ROS/ALIYUN::OLD::Gone"},
                "ProductCode": "old",
                "Name": {"en": "Old Gone", "zh": "旧资源"},
                "Properties": [],
            },
        ],
    )


def test_build_snapshot_uses_list_resource_types_as_authority() -> None:
    snapshot = build_resource_inventory_snapshot(
        api_resource_types=[
            "ALIYUN::ECS::VPC",
            "ALIYUN::POLARDB::DBCluster",
            "ALIYUN::VPC::IpamPool",
        ],
        details_by_type={
            "ALIYUN::ECS::VPC": RosResourceTypeDetail(
                resource_type="ALIYUN::ECS::VPC",
                entity_type="Resource",
                provider="ROS",
                properties={},
                attributes={},
                support_drift_detection=True,
                support_scratch_detection=True,
            ),
            "ALIYUN::VPC::IpamPool": RosResourceTypeDetail(
                resource_type="ALIYUN::VPC::IpamPool",
                entity_type="Resource",
                provider="ROS",
                properties={"IpamPoolName": {"Type": "string", "Description": "The name of the IPAM pool."}},
                attributes={},
            ),
        },
        meta_repository=_meta_repo(),
        fetched_at="2026-06-26T00:00:00Z",
    )

    assert snapshot.api_resource_types == (
        "ALIYUN::ECS::VPC",
        "ALIYUN::POLARDB::DBCluster",
        "ALIYUN::VPC::IpamPool",
    )
    assert snapshot.local_resource_types == (
        "ALIYUN::ECS::VPC",
        "ALIYUN::OLD::Gone",
        "ALIYUN::POLARDB::DBCluster",
    )
    assert snapshot.api_only_resource_types == ("ALIYUN::VPC::IpamPool",)
    assert snapshot.local_only_resource_types == ("ALIYUN::OLD::Gone",)

    vpc = snapshot.items["ALIYUN::ECS::VPC"]
    assert vpc.source_state == "api+local"
    assert vpc.product_code == "ecs"
    assert vpc.category_code == "network"
    assert vpc.name_zh == "专有网络 VPC"

    ipam_pool = snapshot.items["ALIYUN::VPC::IpamPool"]
    assert ipam_pool.source_state == "api-only"
    assert ipam_pool.product_code == "vpc"
    assert ipam_pool.detail is not None
    assert ipam_pool.detail.properties["IpamPoolName"]["Description"] == "The name of the IPAM pool."


class _FakeRosResourceTypeClient:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def list_resource_types(self) -> list[str]:
        return ["ALIYUN::ECS::VPC", "ALIYUN::VPC::SnatEntry", "ALIYUN::VPC::Cached"]

    async def get_resource_type(self, resource_type: str) -> dict[str, Any]:
        self.fetched.append(resource_type)
        if resource_type == "ALIYUN::VPC::SnatEntry":
            raise RuntimeError("temporary throttling")
        return {
            "ResourceType": resource_type,
            "EntityType": "Resource",
            "Provider": "ROS",
            "Properties": {
                "VpcName": {
                    "Type": "string",
                    "Required": False,
                    "Description": "The name of the VPC.",
                }
            },
            "Attributes": {"VpcId": {"Description": "The ID of the VPC."}},
            "SupportDriftDetection": True,
            "SupportScratchDetection": False,
        }


@pytest.mark.asyncio
async def test_collect_inventory_uses_cache_and_records_fetch_errors(tmp_path: Path) -> None:
    cache_path = tmp_path / "ros-resource-types.json"
    cache_path.write_text(
        """{
  "resource_types": ["ALIYUN::VPC::Cached"],
  "details": {
    "ALIYUN::VPC::Cached": {
      "resource_type": "ALIYUN::VPC::Cached",
      "entity_type": "Resource",
      "provider": "ROS",
      "properties": {"CachedId": {"Description": "Cached detail."}},
      "attributes": {},
      "support_drift_detection": false,
      "support_scratch_detection": false
    }
  },
  "errors": {}
}
""",
        encoding="utf-8",
    )
    client = _FakeRosResourceTypeClient()

    snapshot = await collect_ros_resource_inventory(
        client=client,
        cache_path=cache_path,
        meta_repository=_meta_repo(),
        fetched_at="2026-06-26T00:00:00Z",
    )

    assert client.fetched == ["ALIYUN::ECS::VPC", "ALIYUN::VPC::SnatEntry"]
    assert "ALIYUN::VPC::Cached" in snapshot.items
    assert snapshot.items["ALIYUN::VPC::Cached"].detail is not None
    assert snapshot.fetch_errors == {"ALIYUN::VPC::SnatEntry": "temporary throttling"}

    cached = cache_path.read_text(encoding="utf-8")
    assert "ALIYUN::ECS::VPC" in cached
    assert "ALIYUN::VPC::Cached" in cached
    assert "temporary throttling" in cached
    assert "access_key" not in cached.lower()


@pytest.mark.asyncio
async def test_aliyun_inventory_client_receives_only_bound_internal_caller() -> None:
    class BoundInternalCaller:
        def __init__(self) -> None:
            self.calls = []

        async def call(self, *, tool_input, context):
            self.calls.append((tool_input, context))
            if tool_input["action"] == "ListResourceTypes":
                return ToolResult.success('{"ResourceTypes": ["ALIYUN::ECS::VPC"]}')
            return ToolResult.success('{"ResourceType": "ALIYUN::ECS::VPC"}')

    caller = BoundInternalCaller()
    client = AliyunRosResourceTypeClient(caller)

    assert await client.list_resource_types() == ["ALIYUN::ECS::VPC"]
    assert (await client.get_resource_type("ALIYUN::ECS::VPC"))["ResourceType"] == "ALIYUN::ECS::VPC"
    assert [call[0]["action"] for call in caller.calls] == ["ListResourceTypes", "GetResourceType"]
    assert all(call[1].snapshot_id is None for call in caller.calls)
