"""Pipeline loader — reads pipeline.yaml + prompts + skills + hooks from a directory."""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import yaml

from iac_code.agent.system_prompt import DEFAULT_PIPELINE_SECTIONS
from iac_code.pipeline.engine.step_spec import (
    A2AArtifactSpec,
    AllowUserEscapes,
    HandoffContextConfig,
    IncludeExcludeConfig,
    LoadedPipeline,
    OnCompletePolicy,
    StepSpec,
    SubPipelineSpec,
)

logger = logging.getLogger(__name__)

_SELLING_IAC_ALIYUN_REFERENCE_SKILLS = {
    "iac-aliyun-template-generating",
    "iac-aliyun-cost",
    "iac-aliyun-deploying",
}

_SUPPORTED_ON_COMPLETE_ACTIONS = {"switch_to_normal"}
_SUPPORTED_ON_COMPLETE_OUTCOMES = {"completed", "early_exit", "failed", "canceled"}
_SUPPORTED_INTERRUPT_JUDGE_FAILURE_POLICIES = {"continue", "pause", "hard_interrupt"}


def load_pipeline_dir(pipeline_dir: Path) -> LoadedPipeline:
    """Load a complete pipeline from a directory containing pipeline.yaml."""
    yaml_path = pipeline_dir / "pipeline.yaml"
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    name: str = raw["name"]
    context_dependencies: dict[str, list[str]] = raw["context_dependencies"]
    _validate_context_dependencies_acyclic(context_dependencies)
    max_rollbacks: int = raw.get("max_rollbacks", 3)

    raw_sections = raw.get("base_prompt_sections")
    if raw_sections:
        base_prompt_sections = IncludeExcludeConfig(
            include=raw_sections.get("include", []),
            exclude=raw_sections.get("exclude", []),
        )
    else:
        base_prompt_sections = IncludeExcludeConfig(include=list(DEFAULT_PIPELINE_SECTIONS))

    feature_flags = _resolve_feature_flags(raw.get("feature_flags"))
    on_complete = _parse_on_complete(raw.get("on_complete"))

    sub_pipelines = _parse_sub_pipelines(raw.get("sub_pipelines", {}), feature_flags, pipeline_dir)

    steps = _parse_steps(raw["steps"])
    steps = _filter_and_relink(steps, feature_flags)
    _bind_hooks(steps, pipeline_dir)
    _validate_prompts_exist(steps, pipeline_dir)

    skills, skill_schemas, skill_roots = _discover_skills(pipeline_dir)
    _resolve_step_schemas(steps, skill_schemas)
    for sub in sub_pipelines.values():
        _resolve_step_schemas(sub.steps, skill_schemas)
    pipeline_tools = _discover_pipeline_tools(pipeline_dir)

    raw_escapes = raw.get("allow_user_escapes") or {}
    allow_user_escapes = AllowUserEscapes(
        skill=bool(raw_escapes.get("skill", False)),
        command=bool(raw_escapes.get("command", False)),
        shell=bool(raw_escapes.get("shell", False)),
    )

    return LoadedPipeline(
        name=name,
        steps=steps,
        context_dependencies=context_dependencies,
        max_rollbacks=max_rollbacks,
        skills=skills,
        feature_flags=feature_flags,
        sub_pipelines=sub_pipelines,
        base_prompt_sections=base_prompt_sections,
        pipeline_tools=pipeline_tools,
        allow_user_escapes=allow_user_escapes,
        on_complete=on_complete,
        skill_roots=skill_roots,
        emit_stack_events=bool(raw.get("emit_stack_events", False)),
    )


def _resolve_feature_flags(raw_flags: dict | None) -> dict[str, bool]:
    """Resolve feature flags from YAML defaults + environment variable overrides."""
    if not raw_flags:
        return {}
    result: dict[str, bool] = {}
    for flag_name, flag_spec in raw_flags.items():
        default = bool(flag_spec.get("default", True))
        env_var = flag_spec.get("env")
        if env_var:
            env_val = os.environ.get(env_var, "").lower()
            if env_val in ("true", "1", "yes"):
                result[flag_name] = True
            elif env_val in ("false", "0", "no"):
                result[flag_name] = False
            else:
                result[flag_name] = default
        else:
            result[flag_name] = default
    return result


def _validate_context_dependencies_acyclic(dependencies: dict[str, list[str]]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(field_name: str) -> None:
        if field_name in visited:
            return
        if field_name in visiting:
            cycle = visiting[visiting.index(field_name) :] + [field_name]
            raise ValueError(f"context dependency cycle: {' -> '.join(cycle)}")
        visiting.append(field_name)
        for dependency in dependencies.get(field_name, []):
            if dependency in dependencies:
                visit(dependency)
        visiting.pop()
        visited.add(field_name)

    for field_name in dependencies:
        visit(field_name)


def _parse_on_complete(raw: dict | None) -> OnCompletePolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"on_complete must be a mapping, got {raw!r}")

    action = raw.get("action")
    if action not in _SUPPORTED_ON_COMPLETE_ACTIONS:
        supported = ", ".join(sorted(_SUPPORTED_ON_COMPLETE_ACTIONS))
        raise ValueError(f"on_complete.action must be one of: {supported}; got {action!r}")

    raw_apply_on = raw.get("apply_on", ["completed"])
    if not isinstance(raw_apply_on, list) or not all(isinstance(outcome, str) for outcome in raw_apply_on):
        raise ValueError(f"on_complete.apply_on must be a list of strings, got {raw_apply_on!r}")
    apply_on = cast(list[str], raw_apply_on)
    unsupported_outcomes = [outcome for outcome in apply_on if outcome not in _SUPPORTED_ON_COMPLETE_OUTCOMES]
    if unsupported_outcomes:
        supported = ", ".join(sorted(_SUPPORTED_ON_COMPLETE_OUTCOMES))
        unsupported = ", ".join(unsupported_outcomes)
        raise ValueError(f"on_complete.apply_on contains unsupported outcome(s): {unsupported}; supported: {supported}")

    raw_handoff_context = raw["handoff_context"] if "handoff_context" in raw else {}
    if not isinstance(raw_handoff_context, dict):
        raise ValueError(f"on_complete.handoff_context must be a mapping, got {raw_handoff_context!r}")
    raw_include = raw_handoff_context.get("include", [])
    if not isinstance(raw_include, list) or not all(isinstance(field_name, str) for field_name in raw_include):
        raise ValueError(f"on_complete.handoff_context.include must be a list of strings, got {raw_include!r}")
    include = cast(list[str], raw_include)

    return OnCompletePolicy(
        action=action,
        apply_on=list(apply_on),
        handoff_context=HandoffContextConfig(include=list(include)),
    )


def _parse_sub_pipelines(
    raw_subs: dict, feature_flags: dict[str, bool], pipeline_dir: Path
) -> dict[str, SubPipelineSpec]:
    result: dict[str, SubPipelineSpec] = {}
    for sub_name, sub_raw in raw_subs.items():
        steps = _parse_steps(sub_raw.get("steps", []))
        steps = _filter_and_relink(steps, feature_flags)
        _validate_prompts_exist(steps, pipeline_dir)
        result[sub_name] = SubPipelineSpec(
            name=sub_name,
            steps=steps,
            max_rollbacks=sub_raw.get("max_rollbacks", 5),
            iterate_over=sub_raw.get("iterate_over", ""),
            context_fields_from_parent=sub_raw.get("context_fields_from_parent", []),
        )
    return result


def _parse_steps(raw_steps: list[dict]) -> list[StepSpec]:
    steps: list[StepSpec] = []
    for raw in raw_steps:
        raw_tools = raw.get("tools")
        if raw_tools is not None:
            tools = IncludeExcludeConfig(
                include=raw_tools.get("include", []),
                exclude=raw_tools.get("exclude", []),
            )
        else:
            tools = None

        raw_step_sections = raw.get("base_prompt_sections")
        if raw_step_sections:
            step_sections = IncludeExcludeConfig(
                include=raw_step_sections.get("include", []),
                exclude=raw_step_sections.get("exclude", []),
            )
        else:
            step_sections = None

        steps.append(
            StepSpec(
                step_id=raw["id"],
                conclusion_field=raw["conclusion_field"],
                forward=raw.get("forward"),
                prompt_file=raw.get("prompt", ""),
                skill=raw.get("skill"),
                step_type=raw.get("type", "normal"),
                sub_pipeline_name=raw.get("sub_pipeline"),
                tools=tools,
                auto_advance=raw.get("auto_advance", True),
                max_agent_turns=raw.get("max_agent_turns", 50),
                context_fields=raw.get("context_fields", []),
                enabled_when=raw.get("enabled_when"),
                hooks_file=raw.get("hooks_file"),
                base_prompt_sections=step_sections,
                inject_tools=raw.get("inject_tools", []),
                ui_mode=raw.get("ui_mode"),
                conclusion_schema=raw.get("conclusion_schema"),
                max_conclusion_retries=raw.get("max_conclusion_retries", 2),
                interrupt_judge_failure=_parse_interrupt_judge_failure(
                    raw.get("interrupt_judge_failure", "continue"),
                    raw.get("id", "?"),
                ),
                completion_guards=raw.get("completion_guards", []),
                description=raw.get("description", ""),
                exit_condition=_parse_exit_condition(raw.get("exit_condition"), raw.get("id", "?")),
                a2a_artifacts=_parse_a2a_artifacts(raw.get("a2a_artifacts"), raw.get("id", "?")),
            )
        )
    return steps


def _parse_a2a_artifacts(raw: object, step_id: str) -> list[A2AArtifactSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Step '{step_id}': a2a_artifacts must be a list, got {raw!r}")

    specs: list[A2AArtifactSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Step '{step_id}': a2a_artifacts[{index}] must be a dict, got {item!r}")
        item = cast(dict[str, Any], item)
        path = item.get("path") or item.get("source")
        content = item.get("content")
        media_type = item.get("media_type") or item.get("mediaType") or "auto"
        if not isinstance(path, str) or not path:
            raise ValueError(f"Step '{step_id}': a2a_artifacts[{index}].path must be a non-empty string")
        if not isinstance(content, str) or not content:
            raise ValueError(f"Step '{step_id}': a2a_artifacts[{index}].content must be a non-empty string")
        if not isinstance(media_type, str) or not media_type:
            raise ValueError(f"Step '{step_id}': a2a_artifacts[{index}].media_type must be a non-empty string")
        specs.append(A2AArtifactSpec(path=path, content=content, media_type=media_type))
    return specs


def _parse_interrupt_judge_failure(raw: object, step_id: str) -> str:
    if raw not in _SUPPORTED_INTERRUPT_JUDGE_FAILURE_POLICIES:
        supported = ", ".join(sorted(_SUPPORTED_INTERRUPT_JUDGE_FAILURE_POLICIES))
        raise ValueError(f"Step '{step_id}': interrupt_judge_failure must be one of: {supported}; got {raw!r}")
    return cast(str, raw)


def _parse_exit_condition(raw: dict | None, step_id: str) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or "field" not in raw or "value" not in raw:
        raise ValueError(f"Step '{step_id}': exit_condition must be a dict with 'field' and 'value' keys, got {raw!r}")
    return raw


def _filter_and_relink(steps: list[StepSpec], feature_flags: dict[str, bool]) -> list[StepSpec]:
    """Remove disabled steps and fix forward links."""
    enabled = [s for s in steps if _is_enabled(s, feature_flags)]
    enabled_ids = {s.step_id for s in enabled}

    for step in enabled:
        if step.forward and step.forward not in enabled_ids:
            step.forward = _find_next_enabled(steps, step.forward, enabled_ids)

    return enabled


def _is_enabled(step: StepSpec, flags: dict[str, bool]) -> bool:
    if step.enabled_when is None:
        return True
    return flags.get(step.enabled_when, True)


def _find_next_enabled(all_steps: list[StepSpec], start_id: str, enabled_ids: set[str]) -> str | None:
    """Walk forward from start_id to find the next enabled step."""
    steps_by_id = {s.step_id: s for s in all_steps}
    current = start_id
    while current and current not in enabled_ids:
        step = steps_by_id.get(current)
        if step is None:
            return None
        current = step.forward
    return current


def _bind_hooks(steps: list[StepSpec], pipeline_dir: Path) -> None:
    """Load hook files and bind optional step hook callables."""
    for step in steps:
        if not step.hooks_file:
            continue
        hook_path = pipeline_dir / step.hooks_file
        if not hook_path.exists():
            continue
        module = _load_module_from_file(hook_path, f"pipeline_hook_{step.step_id}")
        if hasattr(module, "on_enter"):
            step.on_enter = module.on_enter
        if hasattr(module, "on_exit"):
            step.on_exit = module.on_exit
        if hasattr(module, "on_resource_observed"):
            step.on_resource_observed = module.on_resource_observed
        if hasattr(module, "on_rollback_cleanup_required"):
            step.on_rollback_cleanup_required = module.on_rollback_cleanup_required


def _load_module_from_file(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_prompts_exist(steps: list[StepSpec], pipeline_dir: Path) -> None:
    for step in steps:
        if not step.prompt_file:
            continue
        prompt_path = pipeline_dir / step.prompt_file
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")


def _discover_pipeline_tools(pipeline_dir: Path) -> dict[str, type]:
    """Discover Tool subclasses from <pipeline_dir>/tools/ directory."""
    tools_dir = pipeline_dir / "tools"
    if not tools_dir.is_dir():
        return {}

    from iac_code.tools.base import Tool

    result: dict[str, type] = {}
    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module = _load_module_from_file(py_file, f"pipeline_tool_{py_file.stem}")
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Tool) and attr is not Tool:
                try:
                    instance = attr()
                    result[instance.name] = attr
                except Exception:
                    logger.warning("Failed to instantiate tool %s from %s", attr_name, py_file)
    return result


def _parse_skill_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from a skill markdown file."""
    if not content.startswith("---"):
        return {}
    try:
        end_idx = content.index("---", 3)
    except ValueError:
        return {}
    frontmatter_str = content[3:end_idx].strip()
    return yaml.safe_load(frontmatter_str) or {}


def _discover_skills(pipeline_dir: Path) -> tuple[dict[str, str], dict[str, dict], dict[str, str]]:
    """Discover skills and extract their frontmatter schemas."""
    skills_dir = pipeline_dir / "skills"
    if not skills_dir.is_dir():
        return {}, {}, {}
    contents: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    roots: dict[str, str] = {}
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text(encoding="utf-8")
            contents[skill_dir.name] = content
            roots[skill_dir.name] = str(_skill_prompt_root(skill_dir))
            frontmatter = _parse_skill_frontmatter(content)
            if "conclusion_schema" in frontmatter:
                schemas[skill_dir.name] = frontmatter["conclusion_schema"]
    return contents, schemas, roots


def _skill_prompt_root(skill_dir: Path) -> Path:
    if skill_dir.name not in _SELLING_IAC_ALIYUN_REFERENCE_SKILLS:
        return skill_dir.resolve()
    if (skill_dir / "references").is_dir():
        return skill_dir.resolve()
    try:
        from iac_code.skills.bundled import iac_aliyun

        bundled_root = Path(iac_aliyun.__file__).resolve().parent
    except Exception:
        logger.warning("Failed to resolve bundled iac_aliyun references for %s", skill_dir, exc_info=True)
        return skill_dir.resolve()
    return bundled_root if (bundled_root / "references").is_dir() else skill_dir.resolve()


def _resolve_step_schemas(steps: list[StepSpec], skill_schemas: dict[str, dict]) -> None:
    """Resolve conclusion_schema for each step: explicit YAML > skill frontmatter."""
    for step in steps:
        if step.conclusion_schema is not None:
            continue
        if step.skill and step.skill in skill_schemas:
            step.conclusion_schema = skill_schemas[step.skill]
