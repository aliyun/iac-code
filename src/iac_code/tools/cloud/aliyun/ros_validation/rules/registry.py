"""Built-in rule registry factory."""

from collections.abc import Iterable

from iac_code.tools.cloud.aliyun.ros_validation.facts import FactProvider, ValidationRegistry, ValidationRule


def create_validation_registry(
    providers: Iterable[FactProvider] = (), rules: Iterable[ValidationRule] = ()
) -> ValidationRegistry:
    """Create a dependency-checked built-in/extension registry.

    New independent checks can be added without changing the API hook or the
    expression analyzer's control flow.
    """

    registry = ValidationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    for rule in rules:
        registry.register_rule(rule)
    return registry.freeze()
