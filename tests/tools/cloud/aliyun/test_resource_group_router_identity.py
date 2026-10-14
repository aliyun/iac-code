"""Reviewed Terraform aliases project only their exact fake response rows."""

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import resource_spec, stack_resource_reference


@pytest.mark.parametrize(
    "alias,canonical,field,identifier,scope",
    [
        (
            "alicloud_resource_manager_resource_group",
            "ALIYUN::ResourceManager::ResourceGroup",
            "Id",
            "rg-offline123",
            "account",
        ),
        (
            "alicloud_router_interface",
            "ALIYUN::VPC::RouterInterface",
            "PhysicalResourceId",
            "ri-offline123",
            "regional",
        ),
    ],
)
def test_reviewed_alias_preserves_own_identity(alias, canonical, field, identifier, scope):
    spec = resource_spec(alias)
    assert spec == resource_spec(canonical)
    assert spec.identifier_field == field
    assert spec.scope_kind == scope
    result = stack_resource_reference(
        {"ResourceType": alias, "PhysicalResourceId": identifier},
        stack_id="fake-stack",
        region_id="cn-hangzhou",
        tool_use_id="fake-tool",
        source_path="/Resources/0",
    )
    assert result["resource_type"] == alias
    assert result["identifier"] == identifier


def test_resource_group_projection_uses_account_scope_and_whitelists_attributes():
    row = {
        "Id": "rg-offline123",
        "Name": "offline-name",
        "DisplayName": "offline-display",
        "Status": "OK",
        "Secret": "fake-secret",
    }
    result = extract_api_resource_references(
        product="resourcemanager",
        action="ListResourceGroups",
        params={},
        body={"ResourceGroups": {"ResourceGroup": [row]}},
        region_id="cn-hangzhou",
        tool_use_id="fake-tool",
        account_scope="fake-account",
    )
    assert len(result) == 1
    assert result[0]["identifier"] == row["Id"]
    assert result[0]["scope_kind"] == "account"
    assert "region_id" not in result[0]
    assert result[0]["attributes"] == {k: row[k] for k in ("Id", "Name", "DisplayName", "Status")}


def test_router_interface_projection_keeps_same_row_parent_not_opposite_router():
    row = {
        "RouterInterfaceId": "ri-offline123",
        "RouterId": "vbr-offline123",
        "RouterType": "VBR",
        "OppositeRouterId": "vrt-other123",
        "Secret": "fake-secret",
    }
    result = extract_api_resource_references(
        product="vpc",
        action="DescribeRouterInterfaces",
        params={"RegionId": "cn-hangzhou"},
        body={"RouterInterfaceSet": {"RouterInterfaceType": [row]}},
        region_id="cn-hangzhou",
        tool_use_id="fake-tool",
        account_scope="fake-account",
    )
    ref = next(item for item in result if item["resource_type"] == "ALIYUN::VPC::RouterInterface")
    assert ref["identifier"] == row["RouterInterfaceId"]
    assert ref["region_id"] == "cn-hangzhou"
    assert ref["attributes"] == {
        "PhysicalResourceId": row["RouterInterfaceId"],
        **{k: row[k] for k in ("RouterInterfaceId", "RouterId", "RouterType")},
    }
    assert "fake-secret" not in str(result)


@pytest.mark.parametrize(
    "action,params",
    [
        ("CreateResourceGroup", {"Name": "offline-name", "DisplayName": "offline-display"}),
        ("GetResourceGroup", {"ResourceGroupId": "rg-offline123"}),
    ],
)
def test_resource_group_root_uses_only_response_id_and_same_row_fields(action, params):
    row = {
        "Id": "rg-offline123",
        "Name": "offline-name",
        "DisplayName": "offline-display",
        "Status": "OK",
        "AccountId": "offline-account",
        "Secret": "fake-secret",
    }
    result = extract_api_resource_references(
        product="ResourceManager",
        action=action,
        params=params,
        body={"ResourceGroup": row},
        region_id="cn-hangzhou",
        tool_use_id="fake-tool",
        account_scope="fake-account",
    )
    assert len(result) == 1
    reference = result[0]
    assert reference["resource_type"] == "ALIYUN::ResourceManager::ResourceGroup"
    assert reference["identifier"] == row["Id"]
    assert reference["identifier_field"] == "Id"
    assert reference["identifier_kind"] == "id"
    assert reference["scope_kind"] == "account"
    assert reference["account_scope"] == "fake-account"
    assert "region_id" not in reference
    assert reference["attributes"] == {key: row[key] for key in ("Id", "Name", "DisplayName", "Status")}
    assert reference["evidence"] == "api_response"
    assert reference["sources"][0]["source_path"] == "/result/ResourceGroup/Id"
    assert reference["sources"][0]["tool_use_id"] == "fake-tool"
    assert reference["sources"][0]["action"] == action
    assert "fake-secret" not in str(result)


@pytest.mark.parametrize("action", ["CreateResourceGroup", "GetResourceGroup"])
@pytest.mark.parametrize(
    "identifier",
    [
        None,
        "",
        True,
        123,
        "offline-name",
        "rg-masked****",
        "rg-<unknown>",
        "rg-${identifier}",
        " rg-offline123",
        "rg-offline123\n",
        "https://example.invalid/rg-offline123",
    ],
)
def test_resource_group_roots_reject_missing_or_nonliteral_response_ids(action, identifier):
    params = {"Name": "offline-name", "DisplayName": "offline-display", "ResourceGroupId": "rg-offline123"}
    row = {"Name": "offline-name", "DisplayName": "offline-display", "AccountId": "offline-account"}
    if identifier is not None:
        row["Id"] = identifier
    assert (
        extract_api_resource_references(
            product="resourcemanager",
            action=action,
            params=params,
            body={"ResourceGroup": row},
            region_id="cn-hangzhou",
            tool_use_id="fake-tool",
        )
        == []
    )


@pytest.mark.parametrize("action", ["CreateResourceGroup", "GetResourceGroup"])
@pytest.mark.parametrize(
    "location,field,value",
    [
        ("params", "ResourceGroupId", "rg-other123"),
        ("params", "Id", "rg-other123"),
        ("params", "Name", "another-name"),
        ("params", "DisplayName", "another-display"),
        ("body", "Id", "rg-other123"),
        ("body", "ResourceGroupId", "rg-other123"),
        ("body", "AccountId", "another-account"),
        ("row", "ResourceGroupId", "rg-other123"),
        ("row", "Id", True),
    ],
)
def test_resource_group_roots_reject_conflicting_identity(action, location, field, value):
    params = {"Name": "offline-name", "DisplayName": "offline-display", "ResourceGroupId": "rg-offline123"}
    row = {
        "Id": "rg-offline123",
        "Name": "offline-name",
        "DisplayName": "offline-display",
        "AccountId": "offline-account",
    }
    body = {"ResourceGroup": row}
    {"params": params, "body": body, "row": row}[location][field] = value
    assert (
        extract_api_resource_references(
            product="resourcemanager",
            action=action,
            params=params,
            body=body,
            region_id="cn-hangzhou",
            tool_use_id="fake-tool",
        )
        == []
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ResourceGroup": None},
        {"ResourceGroup": [{"Id": "rg-offline123"}]},
        {"Other": {"ResourceGroup": {"Id": "rg-offline123"}}},
        {"ResourceGroups": {"ResourceGroup": [{"Id": "rg-offline123"}]}},
        {"Code": "NoPermission", "ResourceGroup": {"Id": "rg-offline123"}},
    ],
)
@pytest.mark.parametrize("action", ["CreateResourceGroup", "GetResourceGroup"])
def test_resource_group_roots_do_not_borrow_request_or_unreviewed_objects(action, body):
    assert (
        extract_api_resource_references(
            product="resourcemanager",
            action=action,
            params={"Id": "rg-offline123", "ResourceGroupId": "rg-offline123"},
            body=body,
            region_id="cn-hangzhou",
            tool_use_id="fake-tool",
        )
        == []
    )


def test_resource_group_root_does_not_fill_missing_metadata_from_request():
    result = extract_api_resource_references(
        product="resourcemanager",
        action="CreateResourceGroup",
        params={"Name": "offline-name", "DisplayName": "offline-display", "Id": "rg-offline123"},
        body={"ResourceGroup": {"Id": "rg-offline123"}},
        region_id="cn-hangzhou",
        tool_use_id="fake-tool",
    )
    assert len(result) == 1
    assert result[0]["attributes"] == {"Id": "rg-offline123"}


def test_resource_group_get_requires_matching_request_id():
    assert (
        extract_api_resource_references(
            product="resourcemanager",
            action="GetResourceGroup",
            params={},
            body={"ResourceGroup": {"Id": "rg-offline123"}},
            region_id="cn-hangzhou",
            tool_use_id="fake-tool",
        )
        == []
    )
