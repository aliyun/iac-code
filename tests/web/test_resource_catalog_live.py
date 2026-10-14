from __future__ import annotations

import json
import runpy
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/web/e2e/resource_catalog_live.py"
HELPERS = Path(__file__).parents[2] / "scripts/web/e2e/resource_catalog_live_helpers.mjs"
DIRECT_CATALOG = Path(__file__).parents[2] / "src/iac_code/tools/cloud/aliyun/data/direct_resource_api_catalog.json"


def load_live_catalog_script():
    return runpy.run_path(str(SCRIPT))


def test_targeted_direct_capture_has_fresh_matching_run_and_does_not_overwrite(tmp_path, monkeypatch):
    capture = load_live_catalog_script()["collect_direct_contract"]
    contract_id = "sae/ListApplications/ALIYUN::SAE::Application/Data.Applications.*"
    reference = {
        "identifier": "app-fake",
        "sources": [{"toolUseId": "live-direct-1"}],
    }
    monkeypatch.setitem(
        capture.__globals__,
        "_scan_direct_contract",
        lambda *_args, **_kwargs: {
            "status": "found",
            "regionId": "cn-shenzhen",
            "reference": reference,
            "scannedRegions": 1,
        },
    )
    checkpoint = tmp_path / "source.events.jsonl"
    report = capture(
        object(),
        contract_id=contract_id,
        region_id="cn-shenzhen",
        partition="aliyun",
        direct_contract_timeout=30,
        checkpoint_path=checkpoint,
    )
    events = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert report["scanMode"] == "targeted_direct"
    assert report["target"] == {"contractId": contract_id, "regionId": "cn-shenzhen"}
    assert report["samples"][0]["identifier"] == "app-fake"
    assert [event["phase"] for event in events] == ["scan_start", "direct_api"]
    assert {event["runId"] for event in events} == {report["runId"]}
    with pytest.raises(capture.__globals__["LiveCatalogError"], match="already exists"):
        capture(
            object(),
            contract_id=contract_id,
            region_id="cn-shenzhen",
            partition="aliyun",
            direct_contract_timeout=30,
            checkpoint_path=checkpoint,
        )


def test_direct_catalog_includes_core_runtime_inventory_contracts():
    catalog = json.loads(DIRECT_CATALOG.read_text(encoding="utf-8"))
    identities = {item["resourceType"]: item for item in catalog["resources"]}
    assert identities["ALIYUN::VPC::RouterInterface"]["attributeFields"] == ["RouterId", "RouterType"]
    assert identities["ALIYUN::VPC::SnatEntry"]["parentFields"] == ["NatGatewayId"]
    assert identities["ALIYUN::NAS::ProtocolMountTarget"]["identifierField"] == "ProtocolMountTargetDomain"
    assert identities["ALIYUN::NAS::ProtocolMountTarget"]["parentFields"] == [
        "FileSystemId",
        "ProtocolServiceId",
    ]
    assert identities["ALIYUN::ACTIONTRAIL::Trail"]["identifierField"] == "Name"
    assert identities["ALIYUN::CR::Namespace"]["parentFields"] == ["InstanceId"]
    assert identities["ALIYUN::CR::Repository"]["parentFields"] == ["InstanceId", "RepoNamespaceName"]
    assert identities["ALIYUN::ElasticSearch::Instance"]["identifierField"] == "instanceId"
    contracts = {
        (item["product"], item["action"], item["resourceType"], tuple(item["path"]))
        for item in catalog["responseContracts"]
    }
    assert (
        "alb",
        "ListListeners",
        "ALIYUN::ALB::Listener",
        ("Listeners", "*"),
    ) in contracts
    assert (
        "actiontrail",
        "DescribeTrails",
        "ALIYUN::ACTIONTRAIL::Trail",
        ("TrailList", "*"),
    ) in contracts
    assert (
        "cr",
        "ListNamespace",
        "ALIYUN::CR::Namespace",
        ("Namespaces", "*"),
    ) in contracts
    assert (
        "cr",
        "ListRepository",
        "ALIYUN::CR::Repository",
        ("Repositories", "*"),
    ) in contracts
    assert (
        "elasticsearch",
        "ListInstance",
        "ALIYUN::ElasticSearch::Instance",
        ("Result", "*"),
    ) in contracts
    assert (
        "oss",
        "ListBuckets",
        "ALIYUN::OSS::Bucket",
        ("buckets", "*"),
    ) in contracts
    assert (
        "ecs",
        "DescribeImages",
        "ALIYUN::ECS::Image",
        ("Images", "Image", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeVpcs",
        "ALIYUN::VPC::VPC",
        ("Vpcs", "Vpc", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeVSwitches",
        "ALIYUN::VPC::VSwitch",
        ("VSwitches", "VSwitch", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeCustomerGateways",
        "ALIYUN::VPC::CustomerGateway",
        ("CustomerGateways", "CustomerGateway", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeVpnConnections",
        "ALIYUN::VPC::VpnConnection",
        ("VpnConnections", "VpnConnection", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeRouterInterfaces",
        "ALIYUN::VPC::RouterInterface",
        ("RouterInterfaceSet", "RouterInterfaceType", "*"),
    ) in contracts
    assert (
        "vpc",
        "DescribeSnatTableEntries",
        "ALIYUN::VPC::SnatEntry",
        ("SnatTableEntries", "SnatTableEntry", "*"),
    ) in contracts
    assert (
        "eci",
        "DescribeContainerGroups",
        "ALIYUN::VPC::VPC",
        ("ContainerGroups", "*"),
    ) in contracts
    assert (
        "eci",
        "DescribeContainerGroups",
        "ALIYUN::VPC::VSwitch",
        ("ContainerGroups", "*"),
    ) in contracts


def test_help_parameter_parser_accepts_legacy_and_cli_35_formats():
    parse = load_live_catalog_script()["_parse_help_parameters"]
    assert parse("  --RegionId string Required\n  --PageSize integer Optional\n") == (
        {"RegionId", "PageSize"},
        {"RegionId"},
    )
    assert parse(
        "Required Parameters:\n"
        "  --RegionId                     string\n"
        "                                 Region description\n"
        "Optional Parameters:\n"
        "  --PageSize                     integer\n"
    ) == ({"RegionId", "PageSize"}, {"RegionId"})


def test_local_cli_always_uses_acceptance_profile(monkeypatch):
    module = load_live_catalog_script()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(module["subprocess"], "run", fake_run)
    client = module["AliyunCli"]("/usr/local/bin/aliyun")

    assert client.call(["ecs", "DescribeInstances"]) == {}
    assert calls[0][0] == [
        "/usr/local/bin/aliyun",
        "--profile",
        "test-guima",
        "ecs",
        "DescribeInstances",
    ]


def test_remote_cli_uses_bounded_ssh_command_and_quotes_arguments(monkeypatch):
    module = load_live_catalog_script()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(module["subprocess"], "run", fake_run)
    client = module["AliyunCli"](
        "/tmp/iac-code-resource-e2e/aliyun",
        profile="test-guima",
        ssh_host="root@trusted-host",
        ssh_executable="/usr/bin/ssh",
    )
    assert client.call(["ecs", "DescribeInstances", "--InstanceName", "name with spaces"]) == {}
    command, options = calls[0]
    assert command[:7] == [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "root@trusted-host",
        "env ALIBABA_CLOUD_CLI_NO_COLOR=true "
        "/tmp/iac-code-resource-e2e/aliyun --profile test-guima ecs DescribeInstances "
        "--InstanceName 'name with spaces'",
    ]
    assert options["timeout"] == 45


def test_cli_rejects_non_acceptance_profile():
    module = load_live_catalog_script()

    with pytest.raises(module["LiveCatalogError"], match="test-guima"):
        module["AliyunCli"]("/usr/local/bin/aliyun", profile="current")


def test_direct_context_keeps_ros_parent_attributes_when_direct_sample_follows():
    context = load_live_catalog_script()["_direct_context"](
        [
            {
                "resourceType": "ALIYUN::OTS::Table",
                "reference": {
                    "identifierField": "PhysicalResourceId",
                    "identifier": "ros-table",
                    "attributes": {"InstanceName": "ros-instance"},
                },
            },
            {
                "resourceType": "ALIYUN::OTS::Table",
                "reference": {
                    "identifierField": "PhysicalResourceId",
                    "identifier": "direct-table",
                    "attributes": {"PhysicalResourceId": "direct-table"},
                },
            },
        ]
    )
    assert context["ALIYUN::OTS::Table"] == {
        "instancename": "ros-instance",
        "physicalresourceid": "ros-table",
    }


class FakeDirectClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def help_parameters(self, product, action):
        assert (product, action) == ("clickhouse", "DescribeDBInstances")
        return {"RegionId", "PageNumber"}, {"RegionId"}

    def call(self, arguments):
        self.calls.append(arguments)
        return next(self.responses)


def _clickhouse_sample():
    return {
        "source": "direct",
        "product": "clickhouse",
        "action": "DescribeDBInstances",
        "resourceType": "ALIYUN::ClickHouse::DBCluster",
        "regionId": "cn-hangzhou",
        "identifier": "cc-bp1example",
    }


def test_direct_recheck_marks_existing_resource_as_route_error():
    recheck = load_live_catalog_script()["_recheck_direct_sample"]
    result = recheck(
        FakeDirectClient(
            [
                {
                    "Data": {"DBInstances": [{"DBInstanceId": "cc-bp1example"}]},
                    "TotalCount": 1,
                    "PageSize": 30,
                    "PageNumber": 1,
                }
            ]
        ),
        _clickhouse_sample(),
        signing_region="cn-hangzhou",
        context={},
    )
    assert result == {"status": "route_error", "reason": "resource_still_exists"}


def test_direct_recheck_traverses_all_pages_before_marking_resource_absent():
    recheck = load_live_catalog_script()["_recheck_direct_sample"]
    client = FakeDirectClient(
        [
            {
                "Data": {"DBInstances": [{"DBInstanceId": "cc-first"}]},
                "TotalCount": 2,
                "PageSize": 1,
                "PageNumber": 1,
            },
            {
                "Data": {"DBInstances": [{"DBInstanceId": "cc-second"}]},
                "TotalCount": 2,
                "PageSize": 1,
                "PageNumber": 2,
            },
        ]
    )
    result = recheck(
        client,
        _clickhouse_sample(),
        signing_region="cn-hangzhou",
        context={},
    )
    assert result == {"status": "resource_absent", "reason": "direct_resource_not_listed"}
    assert client.calls[1][-2:] == ["--PageNumber", "2"]


def test_direct_recheck_api_not_found_error_is_inconclusive_not_resource_absence():
    module = load_live_catalog_script()
    recheck = module["_recheck_direct_sample"]
    error_type = module["LiveCatalogError"]

    class FailedDirectClient(FakeDirectClient):
        def call(self, arguments):
            self.calls.append(arguments)
            raise error_type("InvalidAction.NotFound: API not found")

    result = recheck(
        FailedDirectClient([]),
        _clickhouse_sample(),
        signing_region="cn-hangzhou",
        context={},
    )

    assert result == {
        "status": "inconclusive",
        "reason": "direct_recheck_failed",
        "detail": "InvalidAction.NotFound: API not found",
    }


def test_not_found_detection_does_not_confuse_access_denied_with_absence():
    is_not_found = load_live_catalog_script()["_not_found_error"]
    assert is_not_found("InvalidDBClusterId.NotFound: does not exist")
    assert is_not_found("资源不存在")
    assert not is_not_found("Forbidden.RAM: not authorized")


class FakeRosClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def call(self, arguments):
        self.calls.append(arguments)
        return self.response


def _ros_instance_sample():
    return {
        "source": "ros",
        "resourceType": "ALIYUN::ECS::Instance",
        "regionId": "cn-hangzhou",
        "identifier": "i-live123456",
        "reference": {
            "sources": [
                {
                    "stackId": "stack-live",
                    "logicalResourceId": "Server",
                }
            ]
        },
    }


def test_ros_recheck_distinguishes_existing_route_error_from_deleted_resource():
    module = load_live_catalog_script()
    recheck = module["_recheck_ros_sample"]
    region = module["Region"]("cn-hangzhou", "ros.cn-hangzhou.aliyuncs.com")
    common = {
        "ResourceType": "ALIYUN::ECS::Instance",
        "StackId": "stack-live",
        "LogicalResourceId": "Server",
        "PhysicalResourceId": "i-live123456",
    }
    existing = recheck(
        FakeRosClient({**common, "Status": "CREATE_COMPLETE"}),
        _ros_instance_sample(),
        endpoints={"cn-hangzhou": region},
        signing_region="cn-hangzhou",
    )
    deleted = recheck(
        FakeRosClient({**common, "Status": "DELETE_COMPLETE"}),
        _ros_instance_sample(),
        endpoints={"cn-hangzhou": region},
        signing_region="cn-hangzhou",
    )
    assert existing == {"status": "route_error", "reason": "resource_still_exists"}
    assert deleted == {"status": "resource_absent", "reason": "ros_resource_deleted"}


def test_destination_recheck_keeps_observed_route_with_api_classification():
    recheck = load_live_catalog_script()["recheck_destination_failures"]
    href = "https://clickhouse.console.aliyun.com/clickhouse/cn-hangzhou/list/nav/cc-bp1example/basic"
    result = recheck(
        FakeDirectClient(
            [
                {
                    "Data": {"DBInstances": [{"DBInstanceId": "cc-bp1example"}]},
                    "TotalCount": 1,
                    "PageSize": 30,
                    "PageNumber": 1,
                }
            ]
        ),
        {"samples": [_clickhouse_sample()]},
        {
            "destinations": {
                "failures": [
                    {
                        "source": "direct",
                        "resourceType": "ALIYUN::ClickHouse::DBCluster",
                        "identifier": "cc-bp1example",
                        "href": href,
                        "ruleId": "clickhouse-community-cn",
                        "reason": "console_resource_not_found",
                    }
                ]
            }
        },
        signing_region="cn-hangzhou",
    )
    assert result["summary"] == {"routeErrorCount": 1, "resourceAbsentCount": 0, "inconclusiveCount": 0}
    assert result["results"] == [
        {
            "source": "direct",
            "resourceType": "ALIYUN::ClickHouse::DBCluster",
            "identifier": "cc-bp1example",
            "href": href,
            "ruleId": "clickhouse-community-cn",
            "consoleObservation": "console_resource_not_found",
            "status": "route_error",
            "reason": "resource_still_exists",
        }
    ]


def test_browser_destination_helpers_require_exact_locator_tokens(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    script = tmp_path / "resource-catalog-helpers.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        "import {locationContainsIdentifier,textContainsIdentifier,CONSOLE_NOT_FOUND_ERRORS} from "
        + json.dumps(HELPERS.as_uri())
        + ";\n"
        + "assert.equal(locationContainsIdentifier('https://example.test/cn-hangzhou/list','ha'),false);\n"
        + "assert.equal(locationContainsIdentifier('https://example.test/#/detail/ha','ha'),true);\n"
        + "assert.equal(locationContainsIdentifier('https://example.test/list?userId=2048','2048'),true);\n"
        + "assert.equal(textContainsIdentifier('Shanghai resource','ha'),false);\n"
        + "assert.equal(textContainsIdentifier('name: ha (running)','ha'),true);\n"
        + "assert.equal(CONSOLE_NOT_FOUND_ERRORS.some((item)=>item.test('OBJECT_NOT_EXIST')),true);\n",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
