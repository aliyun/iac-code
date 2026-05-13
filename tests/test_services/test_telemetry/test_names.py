"""Tests for telemetry name constants."""

from iac_code.services.telemetry.names import (
    Events,
    GenAiAttr,
    GenAiOperationName,
    GenAiSpanKind,
    Metrics,
    Spans,
)


def test_all_event_constants_start_with_iac_dot():
    for attr in dir(Events):
        if attr.startswith("_"):
            continue
        value = getattr(Events, attr)
        assert isinstance(value, str)
        assert value.startswith("iac."), f"{attr}={value!r}"


def test_all_metric_constants_start_with_iac_dot():
    for attr in dir(Metrics):
        if attr.startswith("_"):
            continue
        value = getattr(Metrics, attr)
        assert isinstance(value, str)
        assert value.startswith("iac.")


def test_all_genai_attr_constants_start_with_gen_ai():
    for attr in dir(GenAiAttr):
        if attr.startswith("_"):
            continue
        value = getattr(GenAiAttr, attr)
        assert isinstance(value, str)
        assert value.startswith("gen_ai."), f"{attr}={value!r}"


def test_genai_span_kind_has_required_values():
    required = {"ENTRY", "LLM", "TOOL", "STEP", "AGENT"}
    defined = {getattr(GenAiSpanKind, a) for a in dir(GenAiSpanKind) if not a.startswith("_")}
    assert required <= defined


def test_genai_operation_name_has_required_values():
    required = {"enter", "chat", "execute_tool", "react"}
    defined = {getattr(GenAiOperationName, a) for a in dir(GenAiOperationName) if not a.startswith("_")}
    assert required <= defined


def test_spec_events_are_all_defined():
    spec_events = {
        "iac.init",
        "iac.session.started",
        "iac.session.exited",
        "iac.session.cancelled",
        "iac.auth.configured",
        "iac.api.request.started",
        "iac.api.request.succeeded",
        "iac.api.request.failed",
        "iac.api.request.retried",
        "iac.model.fallback.triggered",
        "iac.tool.use.succeeded",
        "iac.tool.use.failed",
        "iac.tool.use.granted_in_prompt",
        "iac.tool.use.rejected_in_prompt",
        "iac.template.generated",
        "iac.template.validated",
        "iac.deployment.started",
        "iac.deployment.succeeded",
        "iac.deployment.failed",
        "iac.deployment.cancelled",
        "iac.doc.searched",
        "iac.skill.invoked",
        "iac.skill.completed",
        "iac.aliyun.api.called",
        "iac.memory.compact.succeeded",
        "iac.memory.compact.failed",
        "iac.exception.uncaught",
        "iac.exception.unhandled",
        "iac.query.failed",
    }
    defined = {getattr(Events, a) for a in dir(Events) if not a.startswith("_")}
    missing = spec_events - defined
    assert not missing, f"Missing event constants: {missing}"


def test_spec_metrics_are_all_defined():
    spec_metrics = {
        "iac.session.count",
        "iac.active_time.total",
        "iac.token.usage",
        "iac.api.request.count",
        "iac.api.request.duration",
        "iac.tool.use.count",
        "iac.template.generated.count",
        "iac.template.validated.count",
        "iac.deployment.count",
        "iac.deployment.duration",
        "iac.resource_type.observed.count",
        "iac.aliyun.api.called.count",
        "iac.aliyun.api.called.duration",
    }
    defined = {getattr(Metrics, a) for a in dir(Metrics) if not a.startswith("_")}
    missing = spec_metrics - defined
    assert not missing, f"Missing metric constants: {missing}"


def test_spans_follow_arms_naming():
    assert Spans.ENTRY == "enter_ai_application_system"
    assert Spans.LLM_CHAT == "chat"
    assert Spans.TOOL_EXECUTE == "execute_tool"
    assert Spans.REACT_STEP == "react step"


def test_genai_attr_llm_fields_defined():
    required_attrs = [
        "gen_ai.span.kind",
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.total_tokens",
        "gen_ai.response.finish_reasons",
        "gen_ai.response.time_to_first_token",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.react.round",
        "gen_ai.react.finish_reason",
        "gen_ai.session.id",
        "gen_ai.user.id",
    ]
    defined = {getattr(GenAiAttr, a) for a in dir(GenAiAttr) if not a.startswith("_")}
    for attr in required_attrs:
        assert attr in defined, f"Missing GenAiAttr for {attr}"
