from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from iac_code.tools.base import ToolRegistry
from iac_code.tools.cloud.registry import (
    ANONYMOUS_ALIYUN_TOOL_NAMES,
    CREDENTIAL_GATED_ALIYUN_TOOL_NAMES,
    register_cloud_tools,
)


def runtime_services() -> SimpleNamespace:
    delegated: list[tuple[str, object]] = []
    action_groups: list[tuple[object, object]] = []

    def delegated_executor_factory(action: str) -> object:
        executor = SimpleNamespace(action=action)
        delegated.append((action, executor))
        return executor

    def action_group_executor_factory(spec: object) -> object:
        executor = SimpleNamespace(spec=spec)
        action_groups.append((spec, executor))
        return executor

    return SimpleNamespace(
        openmeta=object(),
        contract_resolver=object(),
        delegated_executor_factory=delegated_executor_factory,
        action_group_executor_factory=action_group_executor_factory,
        delegated=delegated,
        action_groups=action_groups,
    )


def credentials(available: bool) -> MagicMock:
    value = MagicMock()
    value.has_provider.side_effect = lambda provider: available and provider == "aliyun"
    return value


def names(registry: ToolRegistry) -> set[str]:
    return {tool.name for tool in registry.list_tools()}


def test_exact_anonymous_and_credential_gated_groups() -> None:
    assert ANONYMOUS_ALIYUN_TOOL_NAMES == ("aliyun_doc_search", "aliyun_api_doc")
    assert CREDENTIAL_GATED_ALIYUN_TOOL_NAMES == (
        "aliyun_api",
        "ros_validate_template",
        "ros_get_template_parameter_constraints",
        "ros_preview_template",
        "ros_estimate_template_cost",
        "ros_stack",
        "ros_stack_instances",
        "ros_stack_group",
        "ros_template",
        "ros_template_scratch",
        "ros_diagnostic",
        "ros_resource_type_registration",
        "ros_tag",
    )


def test_no_credentials_keeps_exact_anonymous_tools_with_same_services() -> None:
    registry = ToolRegistry()
    services = runtime_services()

    register_cloud_tools(registry, credentials(False), services)

    assert names(registry) == set(ANONYMOUS_ALIYUN_TOOL_NAMES)
    assert registry.get("aliyun_api_doc")._services is services


def test_add_remove_and_repeated_refresh_preserve_anonymous_and_services_identity() -> None:
    registry = ToolRegistry()
    services = runtime_services()
    absent = credentials(False)
    present = credentials(True)

    register_cloud_tools(registry, absent, services)
    anonymous = {name: registry.get(name) for name in ANONYMOUS_ALIYUN_TOOL_NAMES}

    register_cloud_tools(registry, present, services)
    assert names(registry) == set(ANONYMOUS_ALIYUN_TOOL_NAMES + CREDENTIAL_GATED_ALIYUN_TOOL_NAMES)
    assert registry.get("aliyun_api")._runtime_services is services
    assert registry.get("aliyun_api_doc")._services is services
    assert all(registry.get(name) is tool for name, tool in anonymous.items())
    first_delegated = list(services.delegated)
    assert [action for action, _ in first_delegated] == [
        "ValidateTemplate",
        "GetTemplateParameterConstraints",
        "PreviewStack",
        "GetTemplateEstimateCost",
    ]
    assert [spec.public_tool_name for spec, _ in services.action_groups] == [
        "ros_stack_group",
        "ros_template",
        "ros_template_scratch",
        "ros_diagnostic",
        "ros_resource_type_registration",
        "ros_tag",
    ]
    for spec, executor in services.action_groups:
        assert registry.get(spec.public_tool_name)._delegated_executor is executor

    register_cloud_tools(registry, present, services)
    assert names(registry) == set(ANONYMOUS_ALIYUN_TOOL_NAMES + CREDENTIAL_GATED_ALIYUN_TOOL_NAMES)
    assert registry.get("aliyun_api")._runtime_services is services
    assert registry.get("aliyun_api_doc")._services is services
    assert all(registry.get(name) is tool for name, tool in anonymous.items())

    register_cloud_tools(registry, absent, services)
    assert names(registry) == set(ANONYMOUS_ALIYUN_TOOL_NAMES)
    assert all(registry.get(name) is tool for name, tool in anonymous.items())


def test_refresh_removes_stale_execution_tools_even_without_credentials() -> None:
    registry = ToolRegistry()
    services = runtime_services()
    register_cloud_tools(registry, credentials(True), services)

    register_cloud_tools(registry, credentials(False), services)

    assert not set(CREDENTIAL_GATED_ALIYUN_TOOL_NAMES).intersection(names(registry))
    assert set(ANONYMOUS_ALIYUN_TOOL_NAMES).issubset(names(registry))
