"""Convenience base classes for third-party ROS facts and rules."""

from __future__ import annotations

from dataclasses import dataclass

from iac_code.tools.cloud.aliyun.ros_validation.facts import FactKey, RulePhase


@dataclass(frozen=True)
class BaseFactProvider:
    provider_id: str
    phase: RulePhase
    requires: frozenset[FactKey] = frozenset()
    optional_requires: frozenset[FactKey] = frozenset()
    provides: frozenset[FactKey] = frozenset()


@dataclass(frozen=True)
class BaseValidationRule:
    rule_id: str
    phase: RulePhase
    requires: frozenset[FactKey] = frozenset()
    optional_requires: frozenset[FactKey] = frozenset()
