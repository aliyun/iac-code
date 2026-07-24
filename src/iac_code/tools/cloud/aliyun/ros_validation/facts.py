"""Dependency checked FactProvider/ValidationRule execution framework."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    Severity,
    ValidationReport,
    make_diagnostic,
)

FactKey = str


class RulePhase(IntEnum):
    PARSE = 0
    STRUCTURE = 1
    SYMBOLS = 2
    LOCALS_PRECOMPILE = 3
    PRECOMPILE = 4
    EXPRESSIONS = 5
    RESOURCES = 6
    QUALITY = 7


class FactStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    POISONED = "POISONED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FactValue:
    status: FactStatus
    value: Any = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactBuildResult:
    provided: Mapping[FactKey, Any] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()
    poisoned_scopes: frozenset[str] = frozenset()
    incomplete: bool = False


class FactStore:
    def __init__(self) -> None:
        self._values: dict[FactKey, FactValue] = {}

    def publish(self, key: FactKey, value: Any, *, provenance: tuple[str, ...] = ()) -> None:
        if key in self._values:
            raise ValueError("fact already published: {}".format(key))
        self._values[key] = FactValue(FactStatus.AVAILABLE, value, provenance)

    def poison(self, key: FactKey, *, provenance: tuple[str, ...]) -> None:
        if key in self._values:
            raise ValueError("fact already published: {}".format(key))
        self._values[key] = FactValue(FactStatus.POISONED, provenance=provenance)

    def get_required(self, key: FactKey) -> Any:
        fact = self._values.get(key)
        if fact is None or fact.status != FactStatus.AVAILABLE:
            raise KeyError(key)
        return fact.value

    def get_optional(self, key: FactKey) -> FactValue:
        return self._values.get(key, FactValue(FactStatus.UNAVAILABLE))

    def snapshot(self) -> Mapping[FactKey, FactValue]:
        return MappingProxyType(dict(self._values))

    def status(self, key: FactKey) -> FactStatus:
        return self._values.get(key, FactValue(FactStatus.UNAVAILABLE)).status


class FactProvider(Protocol):
    provider_id: str
    phase: RulePhase
    requires: frozenset[FactKey]
    optional_requires: frozenset[FactKey]
    provides: frozenset[FactKey]

    def build(self, context: Any) -> FactBuildResult: ...


class ValidationRule(Protocol):
    rule_id: str
    phase: RulePhase
    requires: frozenset[FactKey]
    optional_requires: frozenset[FactKey]

    def check(self, context: Any) -> Iterable[Diagnostic]: ...


class ValidationRegistry:
    def __init__(self) -> None:
        self.providers: list[FactProvider] = []
        self.rules: list[ValidationRule] = []
        self._frozen = False
        self._provider_order: tuple[FactProvider, ...] = ()

    def register_provider(self, provider: FactProvider) -> None:
        if self._frozen:
            raise RuntimeError("registry is frozen")
        self.providers.append(provider)

    def register_rule(self, rule: ValidationRule) -> None:
        if self._frozen:
            raise RuntimeError("registry is frozen")
        self.rules.append(rule)

    def freeze(self) -> ValidationRegistry:
        provider_ids = [item.provider_id for item in self.providers]
        rule_ids = [item.rule_id for item in self.rules]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("duplicate provider id")
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("duplicate rule id")
        producer: dict[FactKey, FactProvider] = {}
        for provider in self.providers:
            for key in provider.provides:
                if key in producer:
                    raise ValueError("duplicate provider for fact {}".format(key))
                producer[key] = provider
        consumers: list[Any] = [*self.providers, *self.rules]
        for consumer in consumers:
            for key in consumer.requires:
                if key not in producer:
                    raise ValueError("missing provider for required fact {}".format(key))
                if producer[key] is consumer:
                    raise ValueError("provider cannot require its own fact {}".format(key))
                if producer[key].phase > consumer.phase:
                    raise ValueError("future phase dependency for fact {}".format(key))
            for key in consumer.optional_requires:
                if key in producer and producer[key].phase > consumer.phase:
                    raise ValueError("future optional phase dependency for fact {}".format(key))

        edges: dict[str, set[str]] = defaultdict(set)
        indegree: dict[str, int] = {item.provider_id: 0 for item in self.providers}
        for consumer in self.providers:
            for key in consumer.requires | consumer.optional_requires:
                dependency = producer.get(key)
                if dependency is None or dependency is consumer:
                    continue
                if consumer.provider_id not in edges[dependency.provider_id]:
                    edges[dependency.provider_id].add(consumer.provider_id)
                    indegree[consumer.provider_id] += 1
        queue = deque(
            sorted(
                (item for item in self.providers if indegree[item.provider_id] == 0),
                key=lambda item: (item.phase, item.provider_id),
            )
        )
        ordered: list[FactProvider] = []
        by_id = {item.provider_id: item for item in self.providers}
        while queue:
            item = queue.popleft()
            ordered.append(item)
            for target in sorted(edges[item.provider_id]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(by_id[target])
            queue = deque(sorted(queue, key=lambda provider: (provider.phase, provider.provider_id)))
        if len(ordered) != len(self.providers):
            raise ValueError("fact dependency cycle")
        self._provider_order = tuple(ordered)
        self._frozen = True
        return self

    @property
    def provider_order(self) -> tuple[FactProvider, ...]:
        if not self._frozen:
            raise RuntimeError("registry is not frozen")
        return self._provider_order

    def run(self, context: Any) -> ValidationReport:
        """Run every independent provider/rule and suppress poisoned dependants."""

        if not self._frozen:
            self.freeze()
        store: FactStore = context.fact_store
        diagnostics: list[Diagnostic] = []
        incomplete = False

        def internal_error(component_id: str, error: Exception) -> Diagnostic:
            return make_diagnostic(
                code="ROS9999",
                severity=Severity.ERROR,
                category=Category.LIMITATION,
                summary=_("An internal ROS local-validator component failed."),
                detail=_("Component {} raised {}.").format(component_id, type(error).__name__),
                subject=component_id,
                stable_args=(component_id, type(error).__name__),
            )

        for phase in RulePhase:
            for provider in (item for item in self._provider_order if item.phase == phase):
                unavailable = [key for key in provider.requires if store.status(key) != FactStatus.AVAILABLE]
                if unavailable:
                    for key in provider.provides:
                        store.poison(key, provenance=(provider.provider_id, *sorted(unavailable)))
                    incomplete = True
                    continue
                try:
                    result = provider.build(context)
                except Exception as error:  # provider boundary: keep independent phases running
                    diagnostics.append(internal_error(provider.provider_id, error))
                    for key in provider.provides:
                        store.poison(key, provenance=(provider.provider_id, type(error).__name__))
                    incomplete = True
                    continue
                diagnostics.extend(result.diagnostics)
                incomplete = incomplete or result.incomplete
                for key in provider.provides:
                    if key in result.provided:
                        store.publish(key, result.provided[key], provenance=(provider.provider_id,))
                    else:
                        store.poison(key, provenance=(provider.provider_id, *sorted(result.poisoned_scopes)))

            for rule in sorted(
                (item for item in self.rules if item.phase == phase),
                key=lambda item: item.rule_id,
            ):
                if any(store.status(key) != FactStatus.AVAILABLE for key in rule.requires):
                    continue
                try:
                    diagnostics.extend(rule.check(context))
                except Exception as error:  # rule boundary: one extension cannot stop siblings
                    diagnostics.append(internal_error(rule.rule_id, error))
                    incomplete = True

        incomplete = incomplete or bool(getattr(context, "analysis_incomplete", False))
        return ValidationReport.build(diagnostics, analysis_incomplete=incomplete)
