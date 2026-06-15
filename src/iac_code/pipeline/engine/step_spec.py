"""StepSpec — lightweight data-driven step definition."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.types import RollbackRule


@dataclass
class IncludeExcludeConfig:
    """Shared config for include/exclude semantics (tools, base_prompt_sections)."""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class A2AArtifactSpec:
    """A file artifact extracted from a completed step conclusion."""

    path: str
    content: str
    media_type: str = "auto"


@dataclass
class HandoffContextConfig:
    """Context fields to include when handing off from pipeline to normal chat."""

    include: list[str] = field(default_factory=list)


@dataclass
class OnCompletePolicy:
    """Pipeline-level action policy evaluated when the finite pipeline completes."""

    action: str
    apply_on: list[str] = field(default_factory=lambda: ["completed"])
    handoff_context: HandoffContextConfig = field(default_factory=HandoffContextConfig)


@dataclass
class StepSpec:
    """Data-driven step definition. Loaded from pipeline.yaml."""

    step_id: str
    conclusion_field: str
    forward: str | None
    prompt_file: str
    skill: str | None = None
    step_type: str = "normal"
    sub_pipeline_name: str | None = None
    tools: IncludeExcludeConfig | None = None
    rollback_rules: list[RollbackRule] = field(default_factory=list)
    auto_advance: bool = True
    max_agent_turns: int = 50
    context_fields: list[str] = field(default_factory=list)
    enabled_when: str | None = None
    hooks_file: str | None = None
    on_enter: Callable[[PipelineContext], None] | None = None
    on_exit: Callable[[PipelineContext, dict], None] | None = None
    base_prompt_sections: IncludeExcludeConfig | None = None
    inject_tools: list[str] = field(default_factory=list)
    ui_mode: str | None = None
    conclusion_schema: dict | None = None
    max_conclusion_retries: int = 2
    interrupt_judge_failure: str = "continue"
    completion_guards: list[dict] = field(default_factory=list)
    description: str = ""
    exit_condition: dict | None = None
    a2a_artifacts: list[A2AArtifactSpec] = field(default_factory=list)


@dataclass
class SubPipelineSpec:
    """Definition of a reusable sub-pipeline block."""

    name: str
    steps: list[StepSpec]
    max_rollbacks: int
    iterate_over: str
    context_fields_from_parent: list[str] = field(default_factory=list)


@dataclass
class AllowUserEscapes:
    """Pipeline-level toggles for user escape triggers ($/!/slash).

    Each field defaults to False; pipeline.yaml can opt-in per trigger.
    """

    skill: bool = False
    command: bool = False
    shell: bool = False


@dataclass
class LoadedPipeline:
    """Fully resolved pipeline ready for execution."""

    name: str
    steps: list[StepSpec]
    context_dependencies: dict[str, list[str]]
    max_rollbacks: int
    skills: dict[str, str]
    feature_flags: dict[str, bool] = field(default_factory=dict)
    sub_pipelines: dict[str, SubPipelineSpec] = field(default_factory=dict)
    base_prompt_sections: IncludeExcludeConfig = field(default_factory=IncludeExcludeConfig)
    pipeline_tools: dict[str, type] = field(default_factory=dict)
    allow_user_escapes: AllowUserEscapes = field(default_factory=AllowUserEscapes)
    on_complete: OnCompletePolicy | None = None
    skill_roots: dict[str, str] = field(default_factory=dict)
    emit_stack_events: bool = False


def _resolve_dotted(value: object, path: str) -> object:
    """Resolve a dot-separated path against a nested dict/list structure."""
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)  # ty: ignore[invalid-argument-type]
        elif isinstance(value, list) and part.isdigit():
            idx = int(part)
            value = value[idx] if 0 <= idx < len(value) else None
        else:
            return None
        if value is None:
            return None
    return value


def render_prompt(template: str, ctx: PipelineContext, context_fields: list[str]) -> str:
    """Render a prompt template by replacing {field_name} placeholders with context JSON.

    Supports dot-notation: {field.sub_key} resolves into the conclusion dict.
    Uses plain string replacement so that other curly braces in the template
    (e.g. JSON examples) are preserved verbatim.
    """
    result = template
    for field_name in context_fields:
        value = ctx.get_conclusion(field_name)
        replacement = json.dumps(value, ensure_ascii=False, indent=2) if value else "{}"
        result = result.replace("{" + field_name + "}", replacement)

        if value and isinstance(value, dict):
            for key in _collect_dotted_refs(result, field_name):
                sub_value = _resolve_dotted(value, key)
                if sub_value is None:
                    sub_replacement = ""
                elif isinstance(sub_value, str):
                    sub_replacement = sub_value
                else:
                    sub_replacement = json.dumps(sub_value, ensure_ascii=False, indent=2)
                result = result.replace("{" + field_name + "." + key + "}", sub_replacement)
    return result


def _collect_dotted_refs(template: str, prefix: str) -> list[str]:
    """Find all {prefix.xxx.yyy} references in template, return the xxx.yyy parts."""
    import re

    pattern = re.escape(prefix) + r"\.([a-zA-Z0-9_.]+)"
    refs: list[str] = []
    for m in re.finditer(r"\{" + pattern + r"\}", template):
        refs.append(m.group(1))
    return refs
