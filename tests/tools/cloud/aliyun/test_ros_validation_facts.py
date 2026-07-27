from __future__ import annotations

from dataclasses import dataclass, field

from iac_code.tools.cloud.aliyun.ros_validation.facts import (
    FactBuildResult,
    FactStatus,
    FactStore,
    RulePhase,
    ValidationRegistry,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Severity,
    make_diagnostic,
)


@dataclass
class Context:
    fact_store: FactStore = field(default_factory=FactStore)
    ran: list[str] = field(default_factory=list)


def diagnostic(code: str):
    return make_diagnostic(
        code=code,
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=code,
        detail=code,
    )


@dataclass(frozen=True)
class Provider:
    provider_id: str
    phase: RulePhase
    provides: frozenset[str]
    value: object | None = None
    requires: frozenset[str] = frozenset()
    optional_requires: frozenset[str] = frozenset()
    fail: bool = False

    def build(self, context: Context) -> FactBuildResult:
        context.ran.append(self.provider_id)
        if self.fail:
            return FactBuildResult(diagnostics=(diagnostic("ROOT"),), poisoned_scopes=frozenset({self.provider_id}))
        return FactBuildResult(provided={next(iter(self.provides)): self.value})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    phase: RulePhase
    code: str
    requires: frozenset[str] = frozenset()
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: Context):
        context.ran.append(self.rule_id)
        return (diagnostic(self.code),)


def test_poisoned_fact_suppresses_dependent_rule_and_independent_rule_runs() -> None:
    registry = ValidationRegistry()
    registry.register_provider(Provider("broken", RulePhase.PARSE, frozenset({"ast"}), fail=True))
    registry.register_provider(Provider("independent", RulePhase.STRUCTURE, frozenset({"other"}), value=1))
    registry.register_rule(Rule("dependent-rule", RulePhase.EXPRESSIONS, "CASCADE", frozenset({"ast"})))
    registry.register_rule(Rule("independent-rule", RulePhase.EXPRESSIONS, "SIBLING", frozenset({"other"})))
    context = Context()

    report = registry.run(context)

    assert [item.code for item in report.diagnostics] == ["ROOT", "SIBLING"]
    assert "dependent-rule" not in context.ran
    assert context.ran == ["broken", "independent", "independent-rule"]
    # Poisoning a dependent fact suppresses cascades; the explicit root error
    # still constitutes a complete analysis of this synthetic provider graph.
    assert not report.analysis_incomplete


def test_registry_rejects_missing_duplicate_and_future_dependencies() -> None:
    missing = ValidationRegistry()
    missing.register_rule(Rule("r", RulePhase.EXPRESSIONS, "X", frozenset({"missing"})))
    try:
        missing.freeze()
    except ValueError as error:
        assert "missing provider" in str(error)
    else:
        raise AssertionError("missing fact provider was accepted")

    duplicate = ValidationRegistry()
    duplicate.register_provider(Provider("a", RulePhase.PARSE, frozenset({"x"})))
    duplicate.register_provider(Provider("b", RulePhase.PARSE, frozenset({"x"})))
    try:
        duplicate.freeze()
    except ValueError as error:
        assert "duplicate provider" in str(error)
    else:
        raise AssertionError("duplicate fact provider was accepted")

    self_cycle = ValidationRegistry()
    self_cycle.register_provider(Provider("self", RulePhase.PARSE, frozenset({"x"}), requires=frozenset({"x"})))
    try:
        self_cycle.freeze()
    except ValueError as error:
        assert "own fact" in str(error)
    else:
        raise AssertionError("self-dependent fact provider was accepted")


def test_registry_interleaves_rules_with_provider_phases() -> None:
    registry = ValidationRegistry()
    registry.register_provider(Provider("parse-provider", RulePhase.PARSE, frozenset({"parsed"}), value=1))
    registry.register_provider(Provider("quality-provider", RulePhase.QUALITY, frozenset({"quality"}), value=1))
    registry.register_rule(Rule("parse-rule", RulePhase.PARSE, "PARSE", frozenset({"parsed"})))
    registry.register_rule(Rule("quality-rule", RulePhase.QUALITY, "QUALITY", frozenset({"quality"})))
    context = Context()

    registry.run(context)

    assert context.ran == ["parse-provider", "parse-rule", "quality-provider", "quality-rule"]


def test_optional_fact_preserves_unavailable_available_and_poisoned_states() -> None:
    unavailable = FactStore()
    assert unavailable.get_optional("optional").status == FactStatus.UNAVAILABLE

    available = FactStore()
    available.publish("optional", 1, provenance=("provider",))
    assert available.get_optional("optional").status == FactStatus.AVAILABLE
    assert available.get_optional("optional").value == 1

    poisoned = FactStore()
    poisoned.poison("optional", provenance=("provider", "root-error"))
    assert poisoned.get_optional("optional").status == FactStatus.POISONED


def test_registry_rejects_future_optional_dependency_and_provider_cycle() -> None:
    future_optional = ValidationRegistry()
    future_optional.register_provider(Provider("future", RulePhase.QUALITY, frozenset({"future"})))
    future_optional.register_rule(Rule("early", RulePhase.PARSE, "X", optional_requires=frozenset({"future"})))
    try:
        future_optional.freeze()
    except ValueError as error:
        assert "future optional phase" in str(error)
    else:
        raise AssertionError("future optional fact dependency was accepted")

    cycle = ValidationRegistry()
    cycle.register_provider(Provider("a", RulePhase.PARSE, frozenset({"a"}), requires=frozenset({"b"})))
    cycle.register_provider(Provider("b", RulePhase.PARSE, frozenset({"b"}), requires=frozenset({"a"})))
    try:
        cycle.freeze()
    except ValueError as error:
        assert "dependency cycle" in str(error)
    else:
        raise AssertionError("provider dependency cycle was accepted")
