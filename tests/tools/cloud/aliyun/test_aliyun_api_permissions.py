import pytest

from iac_code.services.permissions.audit import fingerprint_text
from iac_code.tools.cloud.aliyun.aliyun_api import AliyunApi
from iac_code.types.permissions import PermissionMode, ToolPermissionContext


def _ctx(*, allow=None, deny=None, ask=None, mode=PermissionMode.DEFAULT):
    return ToolPermissionContext(
        cwd="/tmp",
        allow_rules=allow or {},
        deny_rules=deny or {},
        ask_rules=ask or {},
        mode=mode,
    )


@pytest.mark.asyncio
async def test_read_api_allows() -> None:
    result = await AliyunApi().check_permissions({"product": "ecs", "action": "DescribeInstances"}, _ctx())
    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "read_only"
    assert result.audit.is_read_only is True


@pytest.mark.asyncio
async def test_ros_preview_stack_is_readonly_case_insensitive() -> None:
    result = await AliyunApi().check_permissions({"product": "ROS", "action": "PreviewStack"}, _ctx())
    assert result.behavior == "allow"


@pytest.mark.asyncio
async def test_write_api_asks_with_action_suggestion() -> None:
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, _ctx())
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.suggestions is not None
    assert result.suggestions[0].tool_name == "aliyun_api"
    assert result.suggestions[0].rule_content == "ros:CreateStack"


@pytest.mark.asyncio
async def test_roa_write_method_asks_even_with_read_prefixed_action() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(),
    )

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.is_read_only is False
    assert result.suggestions is not None
    assert result.suggestions[0].tool_name == "aliyun_api"
    assert result.suggestions[0].rule_content == "cs:DescribeClusters"


@pytest.mark.asyncio
async def test_roa_write_method_honors_exact_product_action_allow_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(allow={"session": ["aliyun_api(cs:DescribeClusters)"]}),
    )

    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "session_rule"
    assert result.audit.rule_source == "session"
    assert result.audit.rule == "cs:DescribeClusters"
    assert result.audit.is_read_only is False


@pytest.mark.asyncio
async def test_roa_write_method_requires_exact_allow_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(allow={"session": ["aliyun_api(cs:*)"]}),
    )

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "once"
    assert result.audit.is_read_only is False


@pytest.mark.asyncio
async def test_roa_write_method_still_honors_deny_rule() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "cs",
            "action": "DescribeClusters",
            "style": "ROA",
            "method": "DELETE",
            "pathname": "/clusters/c-123",
        },
        _ctx(deny={"local_settings": ["aliyun_api(cs:DescribeClusters)"]}),
    )

    assert result.behavior == "deny"
    assert result.audit is not None
    assert result.audit.scope == "settings_rule"
    assert result.audit.rule == "cs:DescribeClusters"
    assert result.audit.is_read_only is False


@pytest.mark.parametrize(
    "input",
    [
        {"product": "ros", "action": None},
        {"product": "ros", "action": 123},
        {"product": 123, "action": "CreateStack"},
    ],
)
@pytest.mark.asyncio
async def test_malformed_product_or_action_fails_closed_without_suggestion(input: dict) -> None:
    result = await AliyunApi().check_permissions(input, _ctx())
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.parametrize(
    "product, action",
    [
        ("ro*", "CreateStack"),
        ("ros", "Create:Stack"),
        ("ros", "Create(Stack)"),
        ("ros", "x" * 129),
        ("ro*", "DescribeInstances"),
        ("ros", "Describe:Stack"),
        ("ros", "DescribeInstances token=secret"),
        ("ros", "Get/../../CreateStack"),
    ],
)
@pytest.mark.asyncio
async def test_unsafe_values_do_not_get_persistent_suggestions(product: str, action: str) -> None:
    result = await AliyunApi().check_permissions({"product": product, "action": action}, _ctx())
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.is_read_only is False
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_unsafe_values_are_fingerprinted_in_operation_metadata() -> None:
    result = await AliyunApi().check_permissions(
        {
            "product": "ro*secret",
            "action": "Create:Stack secret",
            "region_id": "cn-hangzhou/secret",
        },
        _ctx(),
    )

    assert result.audit is not None
    assert result.audit.operation == {
        "product_fingerprint": fingerprint_text("ro*secret"),
        "action_fingerprint": fingerprint_text("Create:Stack secret"),
        "region_fingerprint": fingerprint_text("cn-hangzhou/secret"),
    }


@pytest.mark.asyncio
async def test_wildcard_rule_does_not_allow_unsafe_runtime_action() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "Create:Stack"}, context)
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_wildcard_rule_does_not_allow_unsafe_runtime_product() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ro*:CreateStack)"]})
    result = await AliyunApi().check_permissions({"product": "ro*", "action": "CreateStack"}, context)
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.parametrize("input", [{"product": "ros"}, {"action": "CreateStack"}])
@pytest.mark.asyncio
async def test_missing_product_or_action_asks_without_suggestion(input: dict) -> None:
    result = await AliyunApi().check_permissions(input, _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]}))
    assert result.behavior == "ask"
    assert result.suggestions in (None, [])


@pytest.mark.asyncio
async def test_exact_rule_allows_only_matching_action() -> None:
    context = _ctx(allow={"user_settings": ["aliyun_api(ros:CreateStack)"]})
    allowed = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    asked = await AliyunApi().check_permissions({"product": "ros", "action": "DeleteStack"}, context)
    assert allowed.behavior == "allow"
    assert allowed.audit is not None
    assert allowed.audit.scope == "settings_rule"
    assert allowed.audit.rule == "ros:CreateStack"
    assert allowed.audit.rule_source == "user_settings"
    assert asked.behavior == "ask"


@pytest.mark.asyncio
async def test_wildcard_allow_rule_does_not_allow_write_api() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "UpdateStack"}, context)
    assert result.behavior == "ask"


@pytest.mark.asyncio
async def test_wildcard_allow_rule_can_match_read_api() -> None:
    context = _ctx(allow={"project_settings": ["aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "GetStack"}, context)
    assert result.behavior == "allow"
    assert result.audit is not None
    assert result.audit.scope == "settings_rule"
    assert result.audit.rule == "ros:*"
    assert result.audit.rule_source == "project_settings"


@pytest.mark.asyncio
async def test_deny_and_ask_precedence() -> None:
    context = _ctx(
        allow={"user_settings": ["aliyun_api(ros:CreateStack)"]},
        ask={"project_settings": ["aliyun_api(ros:Create*)"]},
        deny={"local_settings": ["aliyun_api(ros:*)"]},
    )
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    assert result.behavior == "deny"
    assert result.audit is not None
    assert result.audit.rule == "ros:*"
    assert result.audit.rule_source == "local_settings"


@pytest.mark.asyncio
async def test_specificity_is_product_first() -> None:
    context = _ctx(ask={"session": ["aliyun_api(ro*:CreateStack)", "aliyun_api(ros:*)"]})
    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)
    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.scope == "session_rule"
    assert result.audit.rule == "ros:*"


@pytest.mark.asyncio
async def test_same_source_equal_specificity_prefers_later_rule() -> None:
    context = _ctx(
        ask={
            "user_settings": [
                "aliyun_api(ro*:CreateStack)",
                "aliyun_api(r*s:CreateStack)",
            ]
        }
    )

    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.rule == "r*s:CreateStack"


@pytest.mark.parametrize(
    ("behavior", "rules_key"),
    [("allow", "allow"), ("deny", "deny"), ("ask", "ask")],
)
@pytest.mark.asyncio
async def test_cli_action_rules_use_cli_scope(behavior: str, rules_key: str) -> None:
    rules = {"cli_arg": ["aliyun_api(ros:CreateStack)"]}
    context = _ctx(**{rules_key: rules})

    result = await AliyunApi().check_permissions({"product": "ros", "action": "CreateStack"}, context)

    assert result.behavior == behavior
    assert result.audit is not None
    assert result.audit.scope == "cli_rule"
    assert result.audit.rule_source == "cli_arg"
    assert result.audit.rule == "ros:CreateStack"


def test_aliyun_api_disables_blanket_allow() -> None:
    assert AliyunApi().supports_blanket_allow is False
