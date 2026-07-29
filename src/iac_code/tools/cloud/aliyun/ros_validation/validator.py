"""Public entry point and staged runner for ROS local template validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.analyzer import ExpressionAnalyzer
from iac_code.tools.cloud.aliyun.ros_validation.facts import (
    FactBuildResult,
    FactStore,
    RulePhase,
)
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    MaterializedTemplateSource,
    RequestValidationContext,
    Severity,
    ValidationPolicy,
    ValidationReport,
    make_diagnostic,
    mapping_segment,
)
from iac_code.tools.cloud.aliyun.ros_validation.parser import parse_template_source
from iac_code.tools.cloud.aliyun.ros_validation.resource_value_specs import (
    DEFAULT_RESOURCE_SPECS,
    ResourceValueSpecRegistry,
)
from iac_code.tools.cloud.aliyun.ros_validation.rules.association_property import (
    AssociationPropertyRule,
    AssociationPropertySpecsProvider,
)
from iac_code.tools.cloud.aliyun.ros_validation.rules.registry import create_validation_registry
from iac_code.tools.cloud.aliyun.ros_validation.symbols import collect_symbols

PARSED_TEMPLATE = "parsed-template"
TEMPLATE_SYMBOLS = "template-symbols"
EXPRESSION_ANALYZER = "expression-analyzer"
LOCALS_PRECOMPILE_FACTS = "locals-precompile-facts"
COUNT_PRECOMPILE_FACTS = "count-precompile-facts"


@dataclass
class _ExecutionContext:
    source: MaterializedTemplateSource
    request: RequestValidationContext
    policy: ValidationPolicy
    resource_specs: ResourceValueSpecRegistry
    parameter_bindings: Mapping[Any, Any]
    fact_store: FactStore = field(default_factory=FactStore)
    analysis_incomplete: bool = False


@dataclass(frozen=True)
class _ParserProvider:
    provider_id: str = "builtin.parser"
    phase: RulePhase = RulePhase.PARSE
    requires: frozenset[str] = frozenset()
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({PARSED_TEMPLATE})

    def build(self, context: _ExecutionContext) -> FactBuildResult:
        result = parse_template_source(
            context.source.text,
            source_id=context.source.origin,
            synthetic_origin=context.source.origin_kind != "SOURCE_TEXT",
        )
        provided = {PARSED_TEMPLATE: result.template} if result.template is not None else {}
        return FactBuildResult(
            provided=provided,
            diagnostics=result.diagnostics,
            poisoned_scopes=frozenset({PARSED_TEMPLATE}) if result.template is None else frozenset(),
            incomplete=result.analysis_incomplete,
        )


@dataclass(frozen=True)
class _SymbolsProvider:
    provider_id: str = "builtin.symbols"
    phase: RulePhase = RulePhase.SYMBOLS
    requires: frozenset[str] = frozenset({PARSED_TEMPLATE})
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({TEMPLATE_SYMBOLS})

    def build(self, context: _ExecutionContext) -> FactBuildResult:
        parsed = context.fact_store.get_required(PARSED_TEMPLATE)
        diagnostics: list[Diagnostic] = []
        if not isinstance(parsed.data, Mapping):
            diagnostics.append(
                make_diagnostic(
                    code="ROS1005",
                    severity=Severity.ERROR,
                    category=Category.COMPATIBILITY,
                    summary=_("The top level of a ROS template must be a Mapping."),
                    detail=_("The current parsed result cannot contain ROS sections."),
                    source_map=parsed.source_map,
                    stable_args=(type(parsed.data).__name__,),
                )
            )
            return FactBuildResult(
                diagnostics=tuple(diagnostics),
                poisoned_scopes=frozenset({TEMPLATE_SYMBOLS}),
            )

        symbols, parameter_errors = collect_symbols(
            parsed.data,
            resource_specs=context.resource_specs,
            evaluation_mode=context.request.evaluation_mode,
            parameter_bindings=context.parameter_bindings,
        )
        for name, error in parameter_errors.items():
            path = (mapping_segment("Parameters"), mapping_segment(name), mapping_segment("Default"))
            diagnostics.append(
                make_diagnostic(
                    code="ROS4102",
                    severity=Severity.ERROR,
                    category=Category.COMPATIBILITY,
                    summary=_("The known value of Parameter {} cannot be parsed as its declared type.").format(name),
                    detail=error,
                    path=path,
                    source_map=parsed.source_map,
                    stable_args=(str(name),),
                )
            )
        return FactBuildResult(provided={TEMPLATE_SYMBOLS: symbols}, diagnostics=tuple(diagnostics))


@dataclass(frozen=True)
class _AnalyzerProvider:
    provider_id: str = "builtin.expression-analyzer"
    phase: RulePhase = RulePhase.SYMBOLS
    requires: frozenset[str] = frozenset({PARSED_TEMPLATE, TEMPLATE_SYMBOLS})
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({EXPRESSION_ANALYZER})

    def build(self, context: _ExecutionContext) -> FactBuildResult:
        parsed = context.fact_store.get_required(PARSED_TEMPLATE)
        symbols = context.fact_store.get_required(TEMPLATE_SYMBOLS)
        analyzer = ExpressionAnalyzer(
            parsed,
            symbols,
            context.resource_specs,
            evaluation_mode=context.request.evaluation_mode,
            semantic_mode=context.request.semantic_mode,
            policy=context.policy,
            parameter_bindings=context.parameter_bindings,
        )
        return FactBuildResult(provided={EXPRESSION_ANALYZER: analyzer})


@dataclass(frozen=True)
class _LocalsPrecompileProvider:
    provider_id: str = "builtin.locals-precompile"
    phase: RulePhase = RulePhase.LOCALS_PRECOMPILE
    requires: frozenset[str] = frozenset({EXPRESSION_ANALYZER})
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({LOCALS_PRECOMPILE_FACTS})

    def build(self, context: _ExecutionContext) -> FactBuildResult:
        analyzer = context.fact_store.get_required(EXPRESSION_ANALYZER)
        diagnostics = tuple(analyzer.analyze_locals_precompile())
        context.analysis_incomplete = context.analysis_incomplete or analyzer.analysis_incomplete
        facts = {
            "locals": analyzer.symbols.locals,
            "phase": RulePhase.LOCALS_PRECOMPILE.name,
        }
        return FactBuildResult(
            provided={LOCALS_PRECOMPILE_FACTS: facts},
            diagnostics=diagnostics,
            incomplete=analyzer.analysis_incomplete,
        )


@dataclass(frozen=True)
class _CountPrecompileProvider:
    provider_id: str = "builtin.count-precompile"
    phase: RulePhase = RulePhase.PRECOMPILE
    requires: frozenset[str] = frozenset({EXPRESSION_ANALYZER, LOCALS_PRECOMPILE_FACTS})
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({COUNT_PRECOMPILE_FACTS})

    def build(self, context: _ExecutionContext) -> FactBuildResult:
        analyzer = context.fact_store.get_required(EXPRESSION_ANALYZER)
        diagnostics = tuple(analyzer.analyze_count_precompile())
        context.analysis_incomplete = context.analysis_incomplete or analyzer.analysis_incomplete
        facts = {
            "resources": analyzer.symbols.resources,
            "count_select_folds": analyzer.count_select_facts,
            "phase": RulePhase.PRECOMPILE.name,
        }
        return FactBuildResult(
            provided={COUNT_PRECOMPILE_FACTS: facts},
            diagnostics=diagnostics,
            incomplete=analyzer.analysis_incomplete,
        )


def _run_analyzer_stage(context: _ExecutionContext, method_name: str) -> tuple[Diagnostic, ...]:
    analyzer = context.fact_store.get_required(EXPRESSION_ANALYZER)
    diagnostics = tuple(getattr(analyzer, method_name)())
    context.analysis_incomplete = context.analysis_incomplete or analyzer.analysis_incomplete
    return diagnostics


@dataclass(frozen=True)
class _StructureRule:
    rule_id: str = "builtin.ros-structure-precompile"
    phase: RulePhase = RulePhase.SYMBOLS
    requires: frozenset[str] = frozenset({EXPRESSION_ANALYZER})
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: _ExecutionContext) -> tuple[Diagnostic, ...]:
        return _run_analyzer_stage(context, "analyze_structure_core")


@dataclass(frozen=True)
class _ConditionRule:
    rule_id: str = "builtin.ros-conditions-rules"
    phase: RulePhase = RulePhase.EXPRESSIONS
    requires: frozenset[str] = frozenset({EXPRESSION_ANALYZER, COUNT_PRECOMPILE_FACTS})
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: _ExecutionContext) -> tuple[Diagnostic, ...]:
        return _run_analyzer_stage(context, "analyze_conditions_and_rules")


@dataclass(frozen=True)
class _ResourceRule:
    rule_id: str = "builtin.ros-resources-outputs"
    phase: RulePhase = RulePhase.RESOURCES
    requires: frozenset[str] = frozenset({EXPRESSION_ANALYZER, COUNT_PRECOMPILE_FACTS})
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: _ExecutionContext) -> tuple[Diagnostic, ...]:
        diagnostics = _run_analyzer_stage(context, "analyze_resources_and_outputs")
        analyzer = context.fact_store.get_required(EXPRESSION_ANALYZER)
        context.analysis_incomplete = context.analysis_incomplete or analyzer.analysis_incomplete
        return diagnostics


_VALIDATION_REGISTRY = create_validation_registry(
    providers=(
        _ParserProvider(),
        AssociationPropertySpecsProvider(),
        _SymbolsProvider(),
        _AnalyzerProvider(),
        _LocalsPrecompileProvider(),
        _CountPrecompileProvider(),
    ),
    rules=(AssociationPropertyRule(), _StructureRule(), _ConditionRule(), _ResourceRule()),
)


def validate_ros_template(
    source: MaterializedTemplateSource,
    request: RequestValidationContext,
    *,
    policy: ValidationPolicy = ValidationPolicy.STRICT,
    resource_specs: ResourceValueSpecRegistry = DEFAULT_RESOURCE_SPECS,
    parameter_bindings: Mapping[Any, Any] | None = None,
) -> ValidationReport:
    context = _ExecutionContext(
        source=source,
        request=request,
        policy=policy,
        resource_specs=resource_specs,
        parameter_bindings=parameter_bindings or {},
    )
    return _VALIDATION_REGISTRY.run(context)
