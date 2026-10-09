"""Extract bounded failure facts from local live artifacts without exporting bodies."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

KNOWN_CODES = (
    "EntityNotExist.Stack", "NotFound.Stack", "StackNotFound", "ActionInProgress",
    "Forbidden", "Forbidden.RAM", "InvalidAccessKeyId.NotFound", "SecurityTokenExpired",
    "Throttling", "Throttling.User", "InvalidParameter", "DeleteFailed", "DependencyViolation",
)
KNOWN_WAITS = {
    "candidate selection visible", "candidate selection controls ready", "candidate selection input ready",
    "live candidate selection controls ready", "pipeline fully completed", "first stack create started",
    "Step 1 ask before restart", "Step 1 ask restored", "Step 2 parameter ask before restart",
    "Step 2 parameter ask restored", "deployment confirmation before restart",
    "deployment confirmation restored", "restored deployment confirmation selector ready",
    "restored Step 2 answer acknowledgement",
    "rollback Step 1 started", "rollback Step 2 started", "rollback Step 3 started",
    "image rollback_completed", "post-image-rollback step_started(intent_parsing)",
    "first Stack accepted creation", "first Stack observed", "rollback cleanup started",
    "restarted old Stack cleanup completion",
    "post-rollback step_started(intent_parsing)", "post-rollback step_started(architecture_planning)",
    "post-rollback step_started(evaluate_candidates)", "post-rollback step_started(confirm_and_select)",
    "post-rollback step_started(deploying)",
}

COMPLETION_ERROR_PATTERNS = {
    "input_schema": r"completion_input_schema_validation_failed",
    "candidate_details_incomplete": r"active candidate batch is fully detailed|rich detail for every candidate",
    "selected_new_batch": r"selected completion is blocked because a new candidate batch was generated",
    "missing_saved_candidates": r"selected completion requires saved authoritative candidates",
    "invalid_selected_index": r"selected_candidate_index must identify one saved candidate",
    "selection_unknown_candidate": r"selected.{0,80}candidate.{0,120}(not found|name mismatch|ambiguous)",
    "candidate_intent_mismatch": r"resource_intents must preserve authoritative intent lifecycle",
    "candidate_decision_notes": r"decision_notes\..{1,40}must list at least",
    "conclusion_schema": r"conclusion_schema_validation_failed|Schema validation failed|schema 验证失败",
    "retry_exhausted": r"maximum retry count|超过最大重试|exceeding.{0,30}retry",
    "no_conclusion": r"No conclusion extracted|No result",
    "model_stream_error": r"No conclusion extracted \(agent stop reason: stream_error\)",
    "model_turn_limit": r"No conclusion extracted \(agent stop reason: max_turns\)",
    "model_output_limit": r"No conclusion extracted \(agent stop reason: (length|max_tokens)\)",
    "missing_required": r"is a required property|required property|缺少必填",
    "guard_rejected": r"completion guard|complete_step validation failed",
    "materialize_validation_stale": r"validate the authoritative candidate output_path after its latest write",
    "materialize_quote_missing": r"ParameterSetAnchor is missing",
    "materialize_quote_region": r"ParameterSetAnchor effective region is unavailable",
    "materialize_overrides_mismatch": r"does not match ParameterSetAnchor",
    "materialize_summary_missing": r"awaiting_confirmation requires a new non-empty solution_summary",
    "materialize_candidate_unavailable": r"authoritative candidate is unavailable",
    "confirmation_template_guard": (
        r"solution_first_confirmed_template_validated|"
        r"A confirmed plan must point at the template file that ros_validate_template validated last|"
        r"确认结论必须指向 ros_validate_template 最后一次校验通过的模板文件"),
    "confirmation_wait_guard": (
        r"solution_first_confirmation_wait_required|"
        r"Deployment can be confirmed only after the current plan was shown|只有当前方案已在专用确认状态中展示后"),
    "confirmation_template_mutated": (
        r"solution_first_revalidate_after_template_write|The confirmed template was rewritten|"
        r"确认使用的模板在 ros_validate_template 之后被改写"),
    "confirmation_parameter_gap": r"confirmed completion cannot contain user-required parameter gaps",
    "hard_constraint_guard": (
        r"hard_constraint_verification_required|Every explicit user hard constraint must be covered|"
        r"每个用户明确提出的硬约束都必须由一条状态为满足的检查覆盖"),
    "natural_handoff_receipt": r"Natural completion did not produce an exact durable handoff receipt",
}

RPC_REQUEST_ERROR_PATTERNS = {
    "workspace_metadata": r"Invalid A2A workspace metadata",
    "empty_input": r"A2A server received empty input",
    "model_image_unsupported": r"does not support image input|不支持.{0,20}(?:图片|图像)",
    "image_part_invalid": r"A2A.{0,50}(?:image|binary|file URL|media type|raw parts)",
    "pipeline_unsupported": r"Unsupported pipeline name|不支持的.{0,15}流水线",
}
CONSTRAINT_ISSUE_CODES = frozenset({
    'constraint_comparison_failed', 'constraint_copy_mismatch', 'constraint_evidence_value_mismatch',
    'constraint_not_satisfied', 'constraint_parameter_mismatch', 'duplicate_constraint_check',
    'invalid_constraint', 'invalid_constraint_check', 'invalid_constraint_checks',
    'invalid_constraint_parameter_values', 'invalid_constraint_source', 'invalid_constraint_verification_mode',
    'invalid_deployment_parameters', 'missing_check_constraint_id', 'missing_constraint_check',
    'missing_constraint_evidence', 'missing_constraint_id', 'missing_tool_evidence',
    'tool_evidence_not_found', 'tool_evidence_value_mismatch', 'unexpected_constraint_check',
})
TERMINAL_ERROR_SIGNATURES = {
    "traceback": r"Traceback \(most recent call last\)",
    "pexpect_exit": r"pexpect\.(?:TIMEOUT|EOF)",
    "prompt_rejection": r"rejected_in_prompt",
    "permission_rejection": r"Permission.*reject|权限.*拒绝",
}


def _pty_terminal_failure_facts(root: Path, runtime_config_dir: Path | None) -> dict[str, Any]:
    """Keep matched error kinds and real source frames, never PTY/tool text."""
    markers: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    frames: set[str] = set()
    origins: Counter[str] = Counter()
    allowed_types = {"ValueError", "TypeError", "RuntimeError", "AttributeError", "KeyError",
                     "PermissionError", "FileNotFoundError", "CancelledError", "TimeoutError"}
    for path in _evidence_paths(root, "transcript.normalized.log", None)[:4]:
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in TERMINAL_ERROR_SIGNATURES.items():
            markers[label] += len(re.findall(pattern, text))
        for block in text.split("Traceback (most recent call last):")[1:][-20:]:
            for line in block.splitlines()[:120]:
                match = re.search(r'File "[^"\n]*[\\/]iac_code[\\/]([A-Za-z0-9_\\/]+\.py)", line ([0-9]{1,6})', line)
                if match:
                    relative = "src/iac_code/" + match.group(1).replace("\\", "/")
                    if (Path(__file__).resolve().parents[2] / relative).is_file():
                        frames.add(relative + ":" + match.group(2))
                match = re.match(r"\s*(?:[A-Za-z_][A-Za-z_0-9]*\.)*([A-Za-z_]+):", line)
                if match and match.group(1) in allowed_types:
                    kinds[match.group(1)] += 1
                    break
    if not any(markers.values()):
        return {}
    for path in _evidence_paths(root, "transcripts/*/session.jsonl", runtime_config_dir)[:30]:
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("content"), list):
                continue
            for block in row["content"]:
                if not isinstance(block, dict):
                    continue
                origin = "tool_result" if block.get("type") == "tool_result" else row.get("role")
                if origin not in {"tool_result", "assistant", "user", "system"}:
                    continue
                body = json.dumps(block.get("content") if origin == "tool_result" else block.get("text"),
                                  ensure_ascii=False)
                for label, pattern in TERMINAL_ERROR_SIGNATURES.items():
                    if re.search(pattern, body):
                        origins[str(origin) + ":" + label] += 1
    return {k: v for k, v in {
        "pty_terminal_error_markers": {k: min(v, 10000) for k, v in markers.items() if v},
        "pty_exception_types": dict(kinds), "pty_source_frames": sorted(frames)[:40],
        "pty_terminal_marker_message_origins": dict(origins),
    }.items() if v}


def _evidence_paths(root: Path, pattern: str, runtime_config_dir: Path | None) -> list[Path]:
    paths: dict[Path, Path] = {}
    for base in (root, runtime_config_dir):
        if base is None:
            continue
        for path in base.rglob(pattern):
            paths.setdefault(path.resolve(), path)
            if len(paths) >= 60:
                return list(paths.values())
    return list(paths.values())


def _schema_property_names() -> set[str]:
    """Only names in the repository's public schema can leave the CI host."""
    pipelines = Path(__file__).resolve().parents[2] / 'src/iac_code/pipeline'
    pending = [yaml.safe_load(path.read_text(encoding='utf-8'))
               for path in sorted(pipelines.glob('*/pipeline.yaml'))]
    names: set[str] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            properties = value.get('properties')
            if isinstance(properties, dict):
                names.update(k for k in properties if isinstance(k, str))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return names


def _schema_type_trace(text: str, inputs: Any, allowed_fields: set[str]) -> list[dict[str, Any]]:
    """Bind a native validation coordinate to its submitted JSON type, never its value."""
    paths = re.findall(r'"path"\s*:\s*"([^"]*)"', text)
    coordinates = [p.strip('/').split('/') for p in paths if p]
    for pointer in re.findall(r'On instance([^:\n]*):', text):
        coordinates.append([name or index for name, index in re.findall(
            r"\['([A-Za-z_][A-Za-z_0-9]*)'\]|\[([0-9]+)\]", pointer)])
    traces = []
    for parts in coordinates:
        if not parts or len(parts) > 16 or any(
            part not in allowed_fields | {'conclusion'} and not part.isdecimal() for part in parts
        ):
            continue
        for source, value in [('tool_input', inputs), ('conclusion', inputs.get('conclusion'))
                              if isinstance(inputs, dict) else ('conclusion', None)]:
            found = True
            for part in parts:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
                    value = value[int(part)]
                else:
                    found = False
                    break
            if not found:
                continue
            kind = ('null' if value is None else 'boolean' if isinstance(value, bool)
                    else 'number' if isinstance(value, (int, float)) else 'string' if isinstance(value, str)
                    else 'array' if isinstance(value, list) else 'object' if isinstance(value, dict) else 'other')
            traces.append({'path': '/'.join('[]' if part.isdecimal() else part for part in parts),
                           'source': source, 'actual_type': kind})
            break
    return traces[:20]


def _saved_constraint_check_facts(root: Path, runtime_config_dir: Path | None) -> list[dict[str, Any]]:
    """Describe current checkpoint checks; they are not a replay of failed tool inputs."""
    from iac_code.pipeline.engine.hard_constraints import constraint_satisfied

    facts: list[dict[str, Any]] = []
    for path in _evidence_paths(root, "pipeline/context.yaml", runtime_config_dir)[:8]:
        try:
            if path.is_symlink() or path.stat().st_size > 2_000_000:
                continue
            context = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        field = context.get('selected_plan') if isinstance(context, dict) else None
        plan = field.get('value') if isinstance(field, dict) else None
        result = plan.get('selected_candidate_result') if isinstance(plan, dict) else None
        cost = result.get('cost') if isinstance(result, dict) else None
        checks = cost.get('hard_constraint_checks') if isinstance(cost, dict) else None
        for index, check in enumerate(checks[:20] if isinstance(checks, list) else []):
            constraint = check.get('constraint') if isinstance(check, dict) else None
            if not isinstance(constraint, dict):
                continue
            item: dict[str, Any] = {'source': 'current_checkpoint', 'check_index': index}
            for key, allowed in {
                'operator': {'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'contains', 'not_contains'},
                'target': {'VPC', 'VSwitch', 'Network', 'ECS', 'SecurityGroup', 'Stack'},
                'verification_mode': {'direct', 'tool'},
            }.items():
                value = constraint.get(key)
                item[key] = value if isinstance(value, str) and value in allowed else 'other'
            status = check.get('status')
            item['llm_status'] = (status if isinstance(status, str) and status in {
                'satisfied', 'conflict', 'unresolved'} else 'other')
            prop = re.sub(r'[^a-z]', '', str(constraint.get('property') or '').lower())
            item['property_kind'] = prop if prop in {'cidrblock', 'vcpu', 'memory', 'zoneid', 'vpcid'} else 'other'
            try:
                item['code_comparison_passed'] = constraint_satisfied(
                    constraint, check.get('actual_value'), actual_unit=check.get('actual_unit'))
            except (TypeError, ValueError):
                item['comparison_unavailable'] = True
            # Restrict hashes and network relations to parsed CIDR values, never
            # arbitrary values, resource IDs, passwords, source_text or evidence.
            if item['property_kind'] == 'cidrblock':
                try:
                    expected, actual = [ipaddress.ip_network(value, strict=False)
                                        for value in (constraint.get('value'), check.get('actual_value'))
                                        if isinstance(value, str)]
                    if expected.version == actual.version:
                        item['actual_is_subnet_of_expected'] = actual.subnet_of(expected)
                        item['expected_is_subnet_of_actual'] = expected.subnet_of(actual)
                        for label, network in [('expected_cidr_hash', expected), ('actual_cidr_hash', actual)]:
                            item[label] = hashlib.sha256(str(network).encode()).hexdigest()
                except (TypeError, ValueError):
                    pass
            facts.append(item)
            if len(facts) >= 20:
                return facts
    return facts



def _failed_constraint_input_facts(inputs: Any, error: str, stage: str) -> list[dict[str, Any]]:
    """Describe checks in the rejected call itself, rather than a later checkpoint."""
    conclusion = inputs.get("conclusion") if isinstance(inputs, dict) else None
    if not isinstance(conclusion, dict):
        return []
    result = conclusion.get("selected_candidate_result")
    cost = result.get("cost") if isinstance(result, dict) else None
    groups = [("conclusion", conclusion.get("hard_constraint_checks")),
              ("candidate_cost", cost.get("hard_constraint_checks") if isinstance(cost, dict) else None)]
    facts = []
    issue_ids = [(code, ids.split(",", 1)[0].strip())
                 for code, ids in re.findall(r"([a-z_]+)\[([^\]]*)\]", error)
                 if code in CONSTRAINT_ISSUE_CODES]
    supplied_checks = 0
    for location, checks in groups:
        for check in checks[:30] if isinstance(checks, list) else []:
            if not isinstance(check, dict):
                continue
            supplied_checks += 1
            constraint = check.get("constraint")
            identity = check.get("constraint_id") or (constraint.get("id") if isinstance(constraint, dict) else None)
            if not isinstance(identity, str) or not identity:
                continue
            issues = sorted({code for code, identifier in issue_ids if identifier == identity})
            if not issues:
                continue
            value = check.get("actual_value")
            kind = ("null" if value is None else "boolean" if isinstance(value, bool)
                    else "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str)
                    else "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "other")
            status = check.get("status")
            item = {"source": "rejected_tool_input", "step": stage, "input_location": location,
                    "issues": issues, "actual_type": kind,
                    "llm_status": status if isinstance(status, str) and status in {
                        "satisfied", "conflict", "unresolved"} else "other"}
            for name in ("parameter_values", "evidence"):
                raw = check.get(name)
                item[name + "_count"] = min(len(raw), 100) if isinstance(raw, (dict, list)) else 0
            # A parsed network hash can identify which quote the rejected input
            # describes without publishing any input text or resource identity.
            if isinstance(value, str):
                try:
                    network = ipaddress.ip_network(value, strict=False)
                except ValueError:
                    pass
                else:
                    item["actual_cidr_hash"] = hashlib.sha256(str(network).encode()).hexdigest()
            facts.append(item)
    if not facts:
        # A sparse `confirmed` delta may inherit checks in the product enricher.
        # Do not mislabel the final checkpoint as the rejected canonical input.
        facts.append({"source": "rejected_tool_input", "step": stage,
                      "checks_supplied_count": min(supplied_checks, 100),
                      "checks_omitted": all(checks is None for _, checks in groups),
                      "matched_check_count": 0,
                      "issues": sorted({code for code, _ in issue_ids})})
    return facts[:30]


def _constraint_delta_facts(conclusion: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve the order and shapes of submitted checks, never their text or IDs."""
    intent = conclusion.get("intent")
    result = conclusion.get("selected_candidate_result")
    cost = result.get("cost") if isinstance(result, dict) else None
    groups = [("intent", intent.get("hard_constraints") if isinstance(intent, dict) else None),
              ("conclusion", conclusion.get("hard_constraint_checks")),
              ("candidate_cost", cost.get("hard_constraint_checks") if isinstance(cost, dict) else None)]
    facts: list[dict[str, Any]] = []
    for location, checks in groups:
        for index, check in enumerate(checks[:20] if isinstance(checks, list) else []):
            if not isinstance(check, dict):
                continue
            item: dict[str, Any] = {"input_location": location, "check_index": index}
            status = check.get("status")
            if location != "intent":
                item["llm_status"] = (status if isinstance(status, str) and status in {
                    "satisfied", "conflict", "unresolved"} else "other")
            constraint = check if location == "intent" else check.get("constraint")
            for label, value in [("actual_cidr_hash", check.get("actual_value")),
                                 ("expected_cidr_hash", constraint.get("value")
                                  if isinstance(constraint, dict) else None)]:
                if not isinstance(value, str):
                    continue
                try:
                    network = ipaddress.ip_network(value, strict=False)
                except ValueError:
                    continue
                item[label] = hashlib.sha256(str(network).encode()).hexdigest()
            facts.append(item)
            if len(facts) >= 20:
                return facts
    return facts


def _cidr_tool_input_facts(name: str, inputs: Any) -> dict[str, Any]:
    """Keep parsed network hashes in native call order, never template or input text."""
    if not isinstance(inputs, dict):
        return {}
    fields = {
        'write_file': ('content',), 'edit_file': ('old_string', 'new_string'),
        'ros_preview_template': ('parameters',), 'ros_estimate_template_cost': ('parameters',),
        'ros_deploy': ('parameters',), 'complete_step': ('conclusion',),
    }.get(name, ())
    facts: dict[str, Any] = {}
    for field in fields:
        value = inputs.get(field)
        # Match CIDR literals only; a string containing an ID/password is never hashed.
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        hashes = set()
        for literal in re.findall(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}(?![\w/])', text):
            try:
                network = ipaddress.IPv4Network(literal, strict=False)
            except ValueError:
                continue
            hashes.add(hashlib.sha256(str(network).encode()).hexdigest())
        if hashes:
            facts[field + '_cidr_hashes'] = sorted(hashes)[:20]
    if name == 'complete_step':
        conclusion = inputs.get('conclusion')
        status = conclusion.get('status') if isinstance(conclusion, dict) else None
        if isinstance(status, str) and status in {'awaiting_confirmation', 'confirmed', 'cancelled'}:
            facts['status'] = status
    return facts


def _completion_failure_facts(root: Path, runtime_config_dir: Path | None = None) -> dict[str, Any]:
    """Project only fixed failure codes and schema validators, never tool result bodies."""
    codes: Counter[str] = Counter()
    validators: set[str] = set()
    missing_fields: set[str] = set()
    schema_fields: set[str] = set()
    schema_types: list[dict[str, Any]] = []
    missing_lifecycles: Counter[str] = Counter()
    intent_sources: Counter[str] = Counter()
    tool_uses: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    step_tool_error_categories: Counter[str] = Counter()
    api_actions: Counter[str] = Counter()
    deployment_inputs: list[dict[str, str]] = []
    intent_stack_names: list[dict[str, str]] = []
    completion_decisions: list[dict[str, Any]] = []
    completion_error_decisions: list[dict[str, Any]] = []
    constraint_issue_counts: Counter[str] = Counter()
    failed_constraint_inputs: list[dict[str, Any]] = []
    cidr_calls: dict[str, dict[str, Any]] = {}
    bash_trace: list[dict[str, Any]] = []
    fixture_instruction_transcripts: set[Path] = set()
    assistant_text_turns = 0
    stages = {"intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select", "deploying",
              "solution_planning_and_selection", "materialize_selected_candidate"}
    transcript_stages: dict[Path, str] = {}
    for meta in _evidence_paths(root, "pipeline/meta.yaml", runtime_config_dir):
        try:
            if meta.stat().st_size > 2_000_000:
                continue
            state = yaml.safe_load(meta.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        attempts = state.get("attempts") if isinstance(state, dict) else None
        items = attempts.get("items") if isinstance(attempts, dict) else None
        for attempt in list(items.values())[:60] if isinstance(items, dict) else []:
            if not isinstance(attempt, dict) or attempt.get("scope") != "parent":
                continue
            step, transcript = attempt.get("step_id"), attempt.get("transcript_id")
            if isinstance(step, str) and step in stages and isinstance(transcript, str) and re.fullmatch(
                r"transcript_att_[0-9]{4,10}", transcript
            ):
                transcript_stages[(meta.parent / "transcripts" / transcript / "session.jsonl").resolve()] = step
    step_tools: Counter[str] = Counter()
    step_text: Counter[str] = Counter()
    denied_tools: Counter[str] = Counter()
    public_tools = {"complete_step", "ask_user_question", "read_memory", "read", "write", "edit", "bash",
                    "read_file", "write_file", "edit_file",
                    "grep", "glob", "aliyun_api", "ros_validate_template", "ros_get_template_parameter_constraints",
                    "ros_preview_template", "ros_estimate_template_cost", "ros_deploy", "show_architecture_diagram",
                    "show_candidate_detail", "select_cloud_resource", "resolve_cloud_resource_selector"}
    allowed_fields = _schema_property_names()
    failed_calls = 0
    detail_errors: Counter[str] = Counter()
    allowed_validators = {"required", "type", "oneOf", "anyOf", "enum", "const", "minItems", "additionalProperties"}
    transcript_paths = _evidence_paths(root, "transcripts/*/session.jsonl", runtime_config_dir)[:30]
    # Filesystem traversal order is not execution order. The persisted attempt
    # counter is monotonic; process its transcripts before unindexed mirrors.
    def attempt_order(path: Path) -> tuple[int, str]:
        match = re.fullmatch(r'transcript_att_([0-9]{4,10})', path.parent.name)
        return (int(match[1]) if match else 10**11, str(path))

    for path in sorted(transcript_paths, key=attempt_order):
        if path.stat().st_size > 20_000_000:
            continue
        calls: set[str] = set()
        call_inputs: dict[str, Any] = {}
        names: dict[str, str] = {}
        bash_calls: dict[str, dict[str, Any]] = {}
        deployment_calls: dict[str, dict[str, Any]] = {}
        stage = transcript_stages.get(path.resolve())
        if stage:
            step_text.setdefault(stage, 0)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            content = row.get("content") if isinstance(row, dict) else None
            if isinstance(row, dict) and row.get("role") in {"system", "user"}:
                texts = ([b.get("text") for b in content if isinstance(b, dict)]
                         if isinstance(content, list) else [content])
                if any(isinstance(t, str) and "# E2E fixture isolation" in t for t in texts):
                    fixture_instruction_transcripts.add(path.resolve())
            if (isinstance(row, dict) and row.get("role") == "assistant" and isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                        and b["text"].strip() for b in content)):
                assistant_text_turns += 1
                if stage:
                    step_text[stage] += 1
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    name = block["name"] if block["name"] in public_tools else "other"
                    tool_uses[name] += 1
                    call_id = block.get('id')
                    if (row.get('role') == 'assistant' and isinstance(call_id, str) and call_id
                        and call_id not in cidr_calls and len(cidr_calls) < 40):
                        cidr = _cidr_tool_input_facts(name, block.get('input'))
                        if cidr:
                            cidr_calls[call_id] = {'tool': name, 'step': stage or 'unknown', **cidr}
                    if row.get("role") == "assistant" and name == "aliyun_api" and isinstance(block.get("input"), dict):
                        action = block["input"].get("action")
                        if action in {"CreateStack", "ContinueCreateStack", "DeleteStack", "GetStack",
                                      "CreateVSwitch", "DeleteVSwitch", "CreateVpc", "CreateSecurityGroup",
                                      "DescribeInstanceTypes"}:
                            api_actions[str(action)] += 1
                    if row.get("role") == "assistant" and name == "bash":
                        inputs = block.get("input")
                        inputs = inputs if isinstance(inputs, dict) else {}
                        command_text = str(inputs.get("command") or "").lstrip()
                        category = next((label for label, pattern in (
                            ("dependency_install",
                             r"(?:uv\s+(?:pip\s+install|sync)|pip[0-9.]*\s+install|npm\s+(?:ci|install))\b"),
                            ("python", r"python[0-9.]*\b|uv\s+run\s+python\b"),
                            ("file_copy", r"(?:cp|copy)\b"),
                            ("aliyun_cli", r"aliyun\b"),
                            ("network_client", r"(?:curl|wget)\b"),
                        ) if re.match(pattern, command_text)), "other")
                        trace = {"step": stage or "unknown", "kind": category, "has_result": False}
                        if isinstance(block.get("id"), str):
                            bash_calls[block["id"]] = trace
                        bash_trace.append(trace)
                        command = json.dumps(block.get("input"), ensure_ascii=False)
                        for action, pattern in {"CreateStack": r"create_stack\s*\(",
                                                "CreateVSwitch": r"create_v_switch\s*\(",
                                                "CreateVpc": r"create_vpc\s*\("}.items():
                            if re.search(pattern, command):
                                api_actions["bash:" + action] += 1
                    if stage:
                        step_tools[stage + ":" + name] += 1
                    if isinstance(block.get("id"), str):
                        names[block["id"]] = name
                    if row.get("role") == "assistant" and name == "ros_deploy" and len(deployment_inputs) < 20:
                        inputs = block.get("input")
                        if isinstance(inputs, dict):
                            projected = {"step": stage or "unknown"}
                            action = inputs.get("action")
                            projected["action"] = action if action in {
                                "create", "continue_create", "delete_and_create", "wait"} else "other"
                            for key in ("stack_name", "stack_id"):
                                value = inputs.get(key)
                                if isinstance(value, str) and value:
                                    projected[key + "_hash"] = hashlib.sha256(value.encode()).hexdigest()
                            parameters = inputs.get('parameters')
                            vpc = parameters.get('VpcId') if isinstance(parameters, dict) else None
                            if isinstance(vpc, str) and vpc:
                                projected['vpc_id_hash'] = hashlib.sha256(vpc.encode()).hexdigest()
                            region = inputs.get('region_id')
                            if isinstance(region, str) and region:
                                projected['region_is_fixture_region'] = region == 'cn-hangzhou'
                            name_value = inputs.get("stack_name")
                            if isinstance(name_value, str) and name_value:
                                suffix = name_value.rsplit("-", 1)[-1].casefold()
                                # Classify recognizable naming examples, never disclose names or suffixes.
                                if suffix in {"a1b2c3", "abc123", "abcdef", "123456", "000000", "test"}:
                                    projected["name_suffix_kind"] = "example_or_placeholder"
                                elif re.fullmatch(r"[0-9]{8}", suffix):
                                    projected["name_suffix_kind"] = "date_only"
                                elif re.fullmatch(r"[a-z0-9]{6,32}", suffix):
                                    projected["name_suffix_kind"] = "alphanumeric"
                                else:
                                    projected["name_suffix_kind"] = "other"
                            deployment_inputs.append(projected)
                            call_id = block.get("id")
                            if isinstance(call_id, str) and call_id:
                                deployment_calls[call_id] = projected
                if block.get("type") == "tool_result":
                    cidr = cidr_calls.get(str(block.get('tool_use_id') or ''))
                    if cidr is not None:
                        cidr['has_result'] = True
                        cidr['is_error'] = block.get('is_error') is True
                    deploy_trace = deployment_calls.get(str(block.get("tool_use_id") or ""))
                    if deploy_trace is not None:
                        deploy_trace["has_result"] = True
                        deploy_trace["is_error"] = block.get("is_error") is True
                        body = block.get("content")
                        if isinstance(body, list):
                            body = "\n".join(str(item.get("text") or "") for item in body if isinstance(item, dict))
                        if isinstance(body, str) and re.search(r"StackExists|AlreadyExists|already exists", body, re.I):
                            deploy_trace["result_category"] = "already_exists"
                    trace = bash_calls.get(str(block.get("tool_use_id") or ""))
                    if trace is not None:
                        trace["has_result"] = True
                        trace["is_error"] = block.get("is_error") is True
                if block.get("type") == "tool_result" and block.get("is_error") is True:
                    name = names.get(str(block.get("tool_use_id") or ""))
                    if name:
                        tool_errors[name] += 1
                        body = block.get("content")
                        if isinstance(body, list):
                            body = '\n'.join(str(item.get('text') or '') for item in body if isinstance(item, dict))
                        if isinstance(body, str):
                            category = next((label for label, pattern in (
                                ("unknown_tool", r"Unknown tool:|未知工具[：:]"),
                                ("invalid_input", r"Invalid input for tool|工具.{0,40}输入无效"),
                                ("permission_denied", r"Permission denied|权限被拒绝|没有权限"),
                            ) if re.search(pattern, body, re.I)), "other")
                            step_tool_error_categories[(stage or "unknown") + ":" + name + ":" + category] += 1
                        if isinstance(body, str) and re.search(
                            r'Permission denied|user explicitly denied|用户.{0,15}拒绝|权限.{0,15}拒绝', body, re.I,
                        ):
                            denied_tools[name] += 1
                if block.get("type") == "tool_use" and block.get("name") == "complete_step":
                    calls.add(str(block.get("id") or ""))
                    call_inputs[str(block.get('id') or '')] = block.get('input')
                    inputs = block.get("input")
                    conclusion = inputs.get("conclusion") if isinstance(inputs, dict) else None
                    if row.get('role') == 'assistant' and isinstance(conclusion, dict):
                        decision = {'step': stage or 'unknown'}
                        constraint_deltas = _constraint_delta_facts(conclusion)
                        if constraint_deltas:
                            decision['submitted_constraint_checks'] = constraint_deltas
                        for flag in ('continue_pipeline', 'is_infra_intent', 'deployment_confirmed'):
                            if type(conclusion.get(flag)) is bool:
                                decision[flag] = conclusion[flag]
                        if isinstance(conclusion.get('status'), str) and conclusion['status'] in {
                            'awaiting_selection', 'selected', 'rejected', 'awaiting_confirmation',
                            'confirmed', 'cancelled', 'reselect_requested',
                        }:
                            decision['status'] = conclusion['status']
                        reason = str(conclusion.get('rejection_reason') or '')
                        for code, pattern in {
                            'cancel': r'取消|cancel',
                            'no_deployment': r'不部署|不创建|not.{0,15}(deploy|creat)|no.{0,15}deploy',
                            'unsupported_vendor': r'AWS|Azure|GCP|非阿里云|不支持.{0,15}云',
                            'not_infrastructure': r'不是.{0,15}(基础设施|云资源)|not.{0,15}infrastructure',
                            'instruction_injection': (
                                r'指令注入|提示词注入|prompt.{0,10}injection|instruction.{0,10}injection'),
                            'missing_information': (
                                r'信息不足|缺少.{0,15}(信息|需求)|insufficient.{0,15}(info|requirement)'),
                            'policy_restriction': r'策略限制|安全限制|policy.{0,15}(restriction|violation)',
                            'unsupported_request': r'不支持.{0,15}(请求|需求)|unsupported.{0,15}(request|requirement)',
                        }.items():
                            if re.search(pattern, reason, re.I):
                                decision.setdefault('rejection_categories', []).append(code)
                        if len(decision) > 1 and len(completion_decisions) < 30:
                            completion_decisions.append(decision)
                    intent = conclusion.get("intent") if isinstance(conclusion, dict) else None
                    if stage == "intent_parsing" and not isinstance(intent, dict) and isinstance(conclusion, dict):
                        intent = conclusion
                    non_functional = intent.get("non_functional") if isinstance(intent, dict) else None
                    stack_name = non_functional.get("stack_name") if isinstance(non_functional, dict) else None
                    if (row.get("role") == "assistant" and isinstance(stack_name, str)
                        and stack_name and len(intent_stack_names) < 20):
                        intent_stack_names.append({"step": stage or "unknown",
                            "stack_name_hash": hashlib.sha256(stack_name.encode()).hexdigest()})
                    constraints = intent.get("hard_constraints") if isinstance(intent, dict) else None
                    for constraint in constraints if isinstance(constraints, list) else []:
                        if not isinstance(constraint, dict) or len(intent_stack_names) >= 20:
                            continue
                        field = re.sub(r"[^a-z0-9]", "", str(constraint.get("property") or "").lower())
                        value = constraint.get("value")
                        if (row.get("role") == "assistant" and field == "stackname"
                            and constraint.get("operator") == "eq" and constraint.get("source") == "user"
                            and isinstance(value, str) and value):
                            intent_stack_names.append({"step": stage or "unknown", "source": "exact_constraint",
                                "stack_name_hash": hashlib.sha256(value.encode()).hexdigest()})
                    rollback = inputs.get("rollback_request") if isinstance(inputs, dict) else None
                    if isinstance(rollback, dict):
                        reason = str(rollback.get("reason") or "")
                        for code, pattern in {
                            "preview_failure": r"preview.{0,25}(fail|失败)|预检.{0,25}失败",
                            "validation_failure": r"validat.{0,25}(fail|失败)|验证.{0,25}失败",
                            "missing_template": r"template.{0,25}(missing|not found)|模板.{0,25}(不存在|缺失)",
                            "constraint_mismatch": r"constraint.{0,25}(mismatch|violat)|约束.{0,25}(不满足|冲突)",
                            "candidate_name_mismatch": r"selected candidate name mismatch|候选.{0,15}名称.{0,10}不匹配",
                            "candidate_not_found": (
                                r"selected.{0,40}candidate.{0,40}not found|候选.{0,15}(未找到|不存在)"),
                            "candidate_invalid": (
                                r"selected candidate payload is missing or invalid|selection_valid.{0,10}false"),
                            "cidr_conflict": r"Cidr.{0,30}(conflict|overlap)|网段.{0,15}(冲突|重叠)",
                        }.items():
                            if re.search(pattern, reason, re.I):
                                codes["rollback_" + code] += 1
                    resources = intent.get("resource_intents") if isinstance(intent, dict) else None
                    for resource in resources if isinstance(resources, list) else []:
                        if not isinstance(resource, dict):
                            continue
                        product, action, source = (resource.get(k) for k in ("product", "action", "source"))
                        if (isinstance(product, str) and product in {
                            "VPC", "VSwitch", "SecurityGroup", "ECS", "FC", "RDS", "SLB", "ALB",
                            "OSS", "EIP", "NATGateway"}
                            and isinstance(action, str) and action in {
                                "create", "use_existing", "reference", "forbid"}):
                            source = source if isinstance(source, str) and source in {
                                "user", "user_explicit", "inferred", "predefined_solution"} else "other"
                            intent_sources[product.casefold() + ":" + action + ":" + source] += 1
                if (block.get("type") == "tool_result" and block.get("is_error")
                    and names.get(block.get("tool_use_id")) == "show_candidate_detail"):
                    body = block.get("content") or ""
                    if isinstance(body, list):
                        body = "\n".join(str(item.get("text") or "") for item in body if isinstance(item, dict))
                    body = body if isinstance(body, str) else ""
                    recognized = False
                    for category, pattern in {
                        "outline_missing": r"before a successful show_architecture_plan",
                        "details_already_complete": r"already have rich details",
                        "index_or_name_mismatch": r"is not allowed yet; expected",
                        "invalid_topology": r"Failed to render the candidate topology",
                        "input_schema": r"is a required property|is not of type|schema_validation_failed",
                    }.items():
                        if re.search(pattern, body, re.I):
                            detail_errors[category] += 1
                            recognized = True
                    if not recognized:
                        detail_errors["other"] += 1
                if (block.get("type") != "tool_result" or block.get("tool_use_id") not in calls
                    or not block.get("is_error")):
                    continue
                failed_calls += 1
                content = block.get('content') or ''
                if isinstance(content, list):
                    content = '\n'.join(str(x.get('text') or '') for x in content if isinstance(x, dict))
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if re.search(COMPLETION_ERROR_PATTERNS['hard_constraint_guard'], text, re.I):
                    # Keep public validator codes, never constraint IDs, values,
                    # evidence bodies, or the error's detail text.
                    constraint_issue_counts.update(set(re.findall(
                        r'\b([a-z_]+)(?=\[|\s*\()', text
                    )).intersection(CONSTRAINT_ISSUE_CODES))
                    if len(failed_constraint_inputs) < 30:
                        failed_constraint_inputs.extend(_failed_constraint_input_facts(
                            call_inputs.get(str(block.get("tool_use_id") or "")), text, stage or "unknown",
                        )[:30 - len(failed_constraint_inputs)])
                try:
                    decoded = json.loads(text)
                except (TypeError, ValueError):
                    pass
                else:
                    if isinstance(decoded, (dict, list)):
                        text = json.dumps(decoded, ensure_ascii=False)
                matched_codes = []
                for code, pattern in COMPLETION_ERROR_PATTERNS.items():
                    if re.search(pattern, text, re.I):
                        codes[code] += 1
                        matched_codes.append(code)
                if len(completion_error_decisions) < 20:
                    inputs = call_inputs.get(str(block.get('tool_use_id') or ''))
                    conclusion = inputs.get('conclusion') if isinstance(inputs, dict) else None
                    status = conclusion.get('status') if isinstance(conclusion, dict) else None
                    if isinstance(status, str) and status in {
                        'awaiting_selection', 'selected', 'rejected', 'awaiting_confirmation',
                        'confirmed', 'cancelled', 'reselect_requested',
                    }:
                        completion_error_decisions.append({
                            'step': stage or 'unknown', 'status': status, 'categories': matched_codes or ['unknown'],
                        })
                if re.search(COMPLETION_ERROR_PATTERNS['candidate_intent_mismatch'], text, re.I):
                    for product, action in re.findall(
                        r"\b(VPC|VSwitch|SecurityGroup|ECS|FC|RDS|SLB|ALB|OSS|EIP|NATGateway):"
                        r"(create|use_existing|reference|forbid)\b", text, re.I
                    ):
                        missing_lifecycles[product.casefold() + ':' + action.casefold()] += 1
                validators.update(v for v in re.findall(r'"validator"\s*:\s*"([A-Za-z]+)"', text)
                                  if v in allowed_validators)
                # A single validation error is plain jsonschema text, without
                # the structured multi-error envelope. Keep its public field
                # name and validator without copying values or messages.
                required = re.findall(r"'([A-Za-z_][A-Za-z_0-9]*)' is a required property", text)
                if required:
                    validators.add('required')
                    missing_fields.update(set(required).intersection(allowed_fields))
                if 'is not of type' in text:
                    validators.add('type')
                if len(schema_types) < 20:
                    for trace in _schema_type_trace(text, call_inputs.get(str(block.get('tool_use_id') or '')),
                                                    allowed_fields):
                        trace['step'] = stage or 'unknown'
                        if trace not in schema_types:
                            schema_types.append(trace)
                            if len(schema_types) >= 20:
                                break
                for pointer in re.findall(r'"path"\s*:\s*"([^"]*)"', text):
                    schema_fields.update(set(pointer.split('/')).intersection(allowed_fields))
                # jsonschema's single-error form uses Python-style instance
                # coordinates rather than the structured error envelope.
                for pointer in re.findall(r'On instance([^:\n]*):', text):
                    schema_fields.update(set(re.findall(r"\['([A-Za-z_][A-Za-z_0-9]*)'\]", pointer))
                                         .intersection(allowed_fields))
    facts: dict[str, Any] = {"complete_step_error_count": min(failed_calls, 10000)}
    if cidr_calls:
        facts['cidr_tool_input_trace'] = list(cidr_calls.values())
    if bash_trace:
        facts["bash_tool_trace"] = bash_trace[-24:]
    if completion_decisions:
        # These are native tool inputs, not proof that validation accepted them.
        facts['completion_decision_inputs'] = completion_decisions
    if completion_error_decisions:
        facts['completion_error_decisions'] = completion_error_decisions
    if failed_constraint_inputs:
        facts['completion_failed_constraint_inputs'] = failed_constraint_inputs
    if constraint_issue_counts:
        facts['completion_constraint_issue_counts'] = dict(constraint_issue_counts)
        saved = _saved_constraint_check_facts(root, runtime_config_dir)
        if saved:
            facts['completion_checkpoint_constraint_checks'] = saved
    if deployment_inputs:
        facts["deployment_input_identity_trace"] = deployment_inputs
    if intent_stack_names:
        facts["completion_intent_stack_name_trace"] = intent_stack_names
    facts["fixture_instruction_transcript_count"] = len(fixture_instruction_transcripts)
    if tool_uses:
        facts["completion_tool_use_counts"] = {k: min(v, 10000) for k, v in sorted(tool_uses.items())}
    if tool_errors:
        facts["completion_tool_error_counts"] = {k: min(v, 10000) for k, v in sorted(tool_errors.items())}
    if step_tool_error_categories:
        facts["completion_step_tool_error_categories"] = {
            k: min(v, 10000) for k, v in sorted(step_tool_error_categories.items())
        }
    if denied_tools:
        facts["permission_denied_tool_counts"] = {k: min(v, 10000) for k, v in sorted(denied_tools.items())}
    if assistant_text_turns:
        facts["completion_assistant_text_turn_count"] = min(assistant_text_turns, 10000)
    if step_tools:
        facts["completion_step_tool_use_counts"] = {k: min(v, 10000) for k, v in sorted(step_tools.items())}
    if step_text:
        facts["completion_step_text_turn_counts"] = {k: min(v, 10000) for k, v in sorted(step_text.items())}
    if detail_errors:
        facts["candidate_detail_error_categories"] = {k: min(v, 10000) for k, v in sorted(detail_errors.items())}
    if codes:
        facts["completion_error_codes"] = dict(codes)
    if validators:
        facts["completion_schema_validators"] = sorted(validators)
    if missing_fields:
        facts['completion_schema_missing_fields'] = sorted(missing_fields)[:20]
    if schema_fields:
        facts['completion_schema_fields'] = sorted(schema_fields)[:20]
    if schema_types:
        facts['completion_schema_value_types'] = schema_types
    if missing_lifecycles:
        facts['candidate_missing_lifecycles'] = dict(missing_lifecycles)
    if intent_sources:
        facts['completion_input_intent_sources'] = dict(intent_sources)
    nudges: dict[str, int] = {}
    for path in sorted(root.glob("server-*.stderr.log"))[:6]:
        if path.stat().st_size > 20_000_000:
            continue
        for step, count in re.findall(
            r"Pipeline step nudge issued: step_id=([a-z_]+) nudge_count=([0-9]{1,2}) max_nudges=[0-9]+ session_id=",
            path.read_text(encoding="utf-8", errors="replace"),
        ):
            if step in stages and 0 < int(count) <= 20:
                nudges[step] = max(nudges.get(step, 0), int(count))
    if nudges:
        facts["completion_nudge_counts"] = nudges
    if api_actions:
        facts["cloud_api_action_counts"] = dict(api_actions)
    return facts


def _known_wait(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value in KNOWN_WAITS:
        return value
    for pattern, label in (
        (r"Step 2 parameter ask #[1-4] input ready", "Step 2 parameter question input ready"),
        (r"deployment confirmation selector ready #[1-9][0-9]?", "deployment confirmation selector ready"),
    ):
        if re.fullmatch(pattern, value):
            return label
    return None


def _cloud_tool_failure_facts(root: Path, runtime_config_dir: Path | None) -> dict[str, Any]:
    from iac_code.pipeline.engine.completion_guard_state import _json_object
    from iac_code.pipeline.selling_solution_first.hooks.materialize_selected_candidate import _quote_projection

    categories: Counter[str] = Counter()
    patterns = {
        "cidr_conflict": r"Cidr.{0,40}Conflict|RouteConflict|CIDR.{0,40}overlap|网段.{0,20}冲突",
        "invalid_cidr": r"InvalidCidrBlock|InvalidVpcCidr|InvalidVSwitchCidr|CidrBlock.{0,30}(?:Invalid|out of)",
        "resource_missing": r"Forbidden\.VpcNotFound|InvalidVpcId\.NotFound|InvalidZoneId\.NotFound",
        "already_exists": r"StackExists|AlreadyExists|already exists",
        "invalid_parameter": r"InvalidParameter",
        "undeclared_parameter": r"ParameterNotFound|UnknownParameter|parameter.{0,80}(?:not defined|not declared|"
                                r"does not exist in.{0,20}template|not found in.{0,20}template)|"
                                r"参数.{0,40}(?:未定义|未声明|不在模板)",
        "quota": r"QuotaExceeded|quota.{0,20}exceed|ExceedQuota",
        "credential": r"InvalidAccessKeyId|SecurityTokenExpired|Forbidden.RAM",
        "throttled": r"Throttling",
        "create_failed": r"CREATE_FAILED",
        "invalid_template": r"InvalidTemplate|InvalidResourceType|InvalidResourceProperty|TemplateFormatError",
        "invalid_database_spec": r"InvalidDBInstance|InvalidEngine|InvalidDBType|InvalidStorage|InvalidCategory",
        "price_unavailable": r"PriceNotFound|ProductNotFound|NoPrice|Unsupported.{0,30}(?:price|product)",
        "unsupported": r"NotSupported|Unsupported|不支持",
        "password_constraint": r"(?:password|密码).{0,80}(?:invalid|constraint|length|must|不合法|长度|必须)",
        "missing_parameter": r"MissingParameter|required parameter|缺少.{0,20}参数",
        "template_not_found": r"template.{0,40}(?:not found|does not exist)|模板.{0,20}不存在",
        "network": r"ConnectionError|ConnectTimeout|ReadTimeout|ConnectionReset|NameResolutionError",
    }
    per_tool: Counter[str] = Counter()
    parameter_names = {"DBInstanceClass", "DBInstanceStorage", "Engine", "EngineVersion", "PayType",
                       "Category", "MasterUsername", "MasterUserPassword", "VpcId", "VSwitchId", "ZoneId",
                       "SecurityIPList", "DBInstanceNetType", "TemplateBody", "TemplatePath", "TemplateId"}
    fields: Counter[str] = Counter()
    quote_shapes: Counter[str] = Counter()
    guard_shapes: Counter[str] = Counter()
    quote_projection_facts: Counter[str] = Counter()
    error_shapes: Counter[str] = Counter()
    zone_failures: Counter[str] = Counter()
    for path in _evidence_paths(root, "transcripts/*/session.jsonl", runtime_config_dir)[:30]:
        try:
            if path.stat().st_size > 20_000_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        cloud_calls: dict[str, str] = {}
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            blocks = row.get("content") if isinstance(row, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if (row.get("role") == "assistant" and block.get("type") == "tool_use"
                    and block.get("name") in {"ros_deploy", "aliyun_api", "ros_preview_template",
                                              "ros_estimate_template_cost", "ros_validate_template"}):
                    cloud_calls[str(block.get("id") or "")] = str(block["name"])
                if (block.get('type') == 'tool_result'
                    and cloud_calls.get(str(block.get('tool_use_id') or '')) == 'ros_estimate_template_cost'):
                    content = block.get('content')
                    if isinstance(content, list):
                        content = '\n'.join(str(b.get('text') or '') for b in content if isinstance(b, dict))
                    guard_content = content
                    metadata = block.get('metadata')
                    external = (
                        metadata.get('_iac_code_externalized_result_path') if isinstance(metadata, dict) else None
                    )
                    if isinstance(external, str):
                        candidate = Path(external)
                        roots = [root] + ([runtime_config_dir] if runtime_config_dir else [])
                        if (candidate.is_file() and not candidate.is_symlink()
                            and any(candidate.resolve().is_relative_to(r.resolve()) for r in roots)
                            and candidate.stat().st_size <= 2_000_000):
                            guard_content = candidate.read_text(encoding='utf-8', errors='replace')
                            guard_shapes['external_result_read'] += 1
                        else:
                            guard_shapes['external_result_unavailable'] += 1
                    parsed = _json_object(guard_content, log_failure=False, allow_ros_preflight_suffix=True)
                    try:
                        projection = _quote_projection({'result': parsed, 'is_error': block.get('is_error') is True})
                    except (ArithmeticError, TypeError, ValueError):
                        projection = {}
                        quote_projection_facts['projection_diagnostic_error'] += 1
                    status = projection.get('quote_status')
                    if status in {'succeeded', 'failed', 'unavailable'}:
                        quote_projection_facts['native_result_projection_' + status] += 1
                    if isinstance(parsed, dict) and isinstance(parsed.get('Resources'), dict):
                        for item in parsed['Resources'].values():
                            if not isinstance(item, dict):
                                quote_projection_facts['resource_not_object'] += 1
                                continue
                            if item.get('Success') is False:
                                quote_projection_facts['resource_marked_failed'] += 1
                                product = {
                                    'ALIYUN::RDS::DBInstance': 'rds',
                                    'ALIYUN::ECS::Instance': 'ecs',
                                    'ALIYUN::ECS::InstanceGroup': 'ecs',
                                    'ALIYUN::ECS::VPC': 'network',
                                    'ALIYUN::VPC::VPC': 'network',
                                    'ALIYUN::ECS::VSwitch': 'network',
                                    'ALIYUN::VPC::VSwitch': 'network',
                                }.get(str(item.get('Type')), 'other')
                                quote_projection_facts['failed_resource_product_' + product] += 1
                                errors = [container[key] for container in (item, item.get('Result'))
                                          if isinstance(container, dict)
                                          for key in ('Error', 'ErrorCode', 'ErrorMessage', 'Code', 'Message',
                                                      'error', 'code', 'message') if key in container]
                                error_text = json.dumps(errors, ensure_ascii=False)
                                matched = [category for category, pattern in patterns.items()
                                           if re.search(pattern, error_text, re.I)]
                                for category in matched or ['unknown' if errors else 'absent']:
                                    quote_projection_facts['failed_resource_error_' + category] += 1
                                for name in parameter_names:
                                    if re.search(r'\b' + name + r'\b', error_text):
                                        quote_projection_facts['failed_resource_error_field_' + name] += 1
                            result = item.get('Result')
                            if not isinstance(result, dict):
                                quote_projection_facts['resource_result_missing'] += 1
                                continue
                            if not isinstance(result.get('Order'), dict):
                                quote_projection_facts['resource_order_missing'] += 1
                            if not isinstance(result.get('OrderSupplement'), dict):
                                quote_projection_facts['resource_supplement_missing'] += 1
                                product = {
                                    'ALIYUN::VPC::EIP': 'eip', 'ALIYUN::VPC::NatGateway': 'nat',
                                    'ALIYUN::ECS::Instance': 'ecs', 'ALIYUN::ECS::InstanceGroup': 'ecs',
                                    'ALIYUN::RDS::DBInstance': 'rds', 'ALIYUN::ECS::VPC': 'network',
                                    'ALIYUN::ECS::VSwitch': 'network',
                                }.get(str(item.get('Type')), 'other')
                                quote_projection_facts['resource_supplement_missing_product_' + product] += 1
                                for container_name, container in (
                                    ('result', result), ('order', result.get('Order')),
                                    ('properties', item.get('Properties')),
                                ):
                                    if isinstance(container, dict):
                                        for field in ('PriceUnit', 'PeriodUnit', 'Period', 'ChargeType',
                                                      'InternetChargeType'):
                                            if field in container:
                                                quote_projection_facts[
                                                    'resource_supplement_missing_' + container_name + '_'
                                                    + field.lower() + '_present'] += 1
                                order = result.get('Order')
                                amounts = [order[k] for k in ('OriginalAmount', 'TradeAmount') if k in order] \
                                    if isinstance(order, dict) else []
                                try:
                                    numbers = [Decimal(str(v)) for v in amounts if not isinstance(v, bool)]
                                    if (not numbers or len(numbers) != len(amounts)
                                        or not all(v.is_finite() and v >= 0 for v in numbers)):
                                        category = 'invalid_amount'
                                    else:
                                        category = 'zero_amount' if all(v == 0 for v in numbers) else 'nonzero_amount'
                                except (InvalidOperation, ValueError):
                                    category = 'invalid_amount'
                                quote_projection_facts['resource_supplement_missing_' + category] += 1
                            order = result.get('Order')
                            if isinstance(order, dict):
                                if any(key in order for key in ('OriginalAmount', 'TradeAmount')):
                                    quote_projection_facts['resource_amount_present'] += 1
                                currency = order.get('Currency')
                                if currency:
                                    quote_projection_facts[
                                        'resource_currency_cny' if str(currency).upper() == 'CNY'
                                        else 'resource_currency_other'] += 1
                            supplement = result.get('OrderSupplement')
                            if isinstance(supplement, dict):
                                for key in ('PriceUnit', 'PeriodUnit', 'Period'):
                                    if key in supplement:
                                        quote_projection_facts['resource_' + key.lower() + '_present'] += 1
                    if parsed is None:
                        guard_shapes['unparsed'] += 1
                    else:
                        resources = parsed.get('Resources')
                        label = ('resources_array' if isinstance(resources, list) else
                                 'resources_object' if isinstance(resources, dict) else 'resources_missing')
                        guard_shapes[label] += 1
                        if any(isinstance(parsed.get(k), (int, float, str))
                               for k in ('OriginalAmount', 'TradeAmount')):
                            guard_shapes['amount_present'] += 1
                    decoded = content
                    if isinstance(content, str):
                        try:
                            decoded = json.loads(content)
                        except ValueError:
                            decoded = None
                            quote_shapes['non_json_text'] += 1
                            if re.search(r'tool-results|externalized|saved to|完整.{0,15}(文件|保存)', content, re.I):
                                quote_shapes['external_result_reference'] += 1
                    if isinstance(decoded, dict):
                        quote_shapes['json_object'] += 1
                        for key in ('Resources', 'result', 'Result', 'data', 'body', 'content', 'Code', 'code',
                                    'error', 'is_success', 'success', 'message', 'Message'):
                            if key in decoded:
                                quote_shapes[key] += 1
                        for key in ('success', 'is_success'):
                            if decoded.get(key) is False:
                                quote_shapes['failure_boolean'] += 1
                    elif isinstance(decoded, list):
                        quote_shapes['json_array'] += 1
                if (block.get("type") == "tool_result" and block.get("tool_use_id") in cloud_calls
                    and block.get("is_error") is True):
                    text = json.dumps(block.get("content"), ensure_ascii=False)
                    native_content = block.get('content')
                    if isinstance(native_content, list):
                        native_content = '\n'.join(str(b.get('text') or '') for b in native_content
                                                   if isinstance(b, dict) and b.get('type') == 'text')
                    metadata = block.get('metadata')
                    external = (
                        metadata.get('_iac_code_externalized_result_path') if isinstance(metadata, dict) else None
                    )
                    if isinstance(external, str):
                        candidate = Path(external)
                        roots = [root] + ([runtime_config_dir] if runtime_config_dir else [])
                        try:
                            if (candidate.is_file() and not candidate.is_symlink()
                                and any(candidate.resolve().is_relative_to(r.resolve()) for r in roots)
                                and candidate.stat().st_size <= 2_000_000):
                                native_content = candidate.read_text(encoding='utf-8', errors='replace')
                                text += '\n' + native_content
                                error_shapes['external_result_read'] += 1
                            else:
                                error_shapes['external_result_unavailable'] += 1
                        except OSError:
                            error_shapes['external_result_unavailable'] += 1
                    matched = [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]
                    categories.update(matched or ["unknown"])
                    tool = cloud_calls[block["tool_use_id"]]
                    per_tool.update(f"{tool}:{category}" for category in matched or ["unknown"])
                    fields.update(name for name in parameter_names if re.search(r"\b" + name + r"\b", text))
                    if tool == 'ros_deploy':
                        parsed = _json_object(native_content, log_failure=False)
                        # Use only the native failed Stack's reason, not template
                        # schemas or model prose that happen to mention ZoneId.
                        reason = parsed.get('status_reason') if isinstance(parsed, dict) else None
                        if isinstance(reason, str) and re.search(r'ZoneId|可用区', reason, re.I):
                            matched_zone = [category for category, pattern in {
                                'missing': r'MissingParameter|mandatory|required|不能为空|必填',
                                'not_found': r'InvalidZoneId\.NotFound|does not exist|not found|不存在',
                                'disabled': r'ZoneIsDisabled|Forbidden\.Zone|disabled|不可用',
                                'unsupported': r'NotSupported|Unsupported|not supported|不支持',
                                'invalid': r'InvalidZoneId|InvalidParameter|invalid|不合法|无效',
                            }.items() if re.search(pattern, reason, re.I)]
                            zone_failures.update(matched_zone or ['unknown'])
    facts = {}
    if categories:
        facts.update(cloud_tool_error_categories=dict(categories), cloud_tool_error_by_tool=dict(per_tool),
                     cloud_tool_error_parameter_fields=dict(fields))
    if quote_shapes:
        facts['quote_native_result_shapes'] = dict(quote_shapes)
    if guard_shapes:
        facts['quote_guard_result_shapes'] = dict(guard_shapes)
    if quote_projection_facts:
        facts['quote_response_diagnostics'] = dict(quote_projection_facts)
    if error_shapes:
        facts['cloud_tool_error_result_shapes'] = dict(error_shapes)
    if zone_failures:
        facts['deployment_zone_failure_categories'] = dict(zone_failures)
    return facts


def _ros_stack_failure_facts(root: Path) -> dict[str, Any]:
    """Project native stack outcomes before teardown, never resource identities or API bodies."""
    statuses: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    template_inspections: Counter[str] = Counter()
    vpc_reference_kinds: Counter[str] = Counter()
    vpc_reference_hashes: set[str] = set()
    zone_reference_kinds: Counter[str] = Counter()
    zone_region_categories: Counter[str] = Counter()
    vpc_presence: Counter[str] = Counter()
    known = {"CREATE_COMPLETE", "CREATE_FAILED", "CREATE_IN_PROGRESS", "ROLLBACK_FAILED", "ROLLBACK_COMPLETE",
             "DELETE_COMPLETE", "DELETE_FAILED", "DELETE_IN_PROGRESS"}
    patterns = {
        "cidr_conflict": r"RouteConflict|Cidr.{0,40}Conflict|CIDR.{0,40}overlap|网段.{0,20}冲突",
        "invalid_cidr": r"InvalidCidrBlock|InvalidVpcCidr|InvalidVSwitchCidr|CidrBlock.{0,30}(?:Invalid|out of)",
        "quota": r"QuotaExceeded|quota.{0,20}exceed|ExceedQuota",
        "permission": r"Forbidden(?!\.(?:VpcNotFound|OperateShareResource)\b)|AccessDenied|PermissionDenied",
        "shared_resource": r"Forbidden\.OperateShareResource\b",
        "resource_missing": r"Forbidden\.VpcNotFound|InvalidVpcId\.NotFound|InvalidZoneId\.NotFound",
        "operation_conflict": r"TaskConflict|IncorrectVSwitchStatus|IncorrectVpcStatus",
        "invalid_parameter": r"InvalidParameter",
        "dependency": r"DependencyViolation",
    }
    for path in list(root.rglob("*.ros-stack-states.json"))[:32]:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            states = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for state in states.values() if isinstance(states, dict) else []:
            if not isinstance(state, dict):
                continue
            diagnostic = state.get('vpc_reference_diagnostic')
            if isinstance(diagnostic, dict):
                inspected = diagnostic.get('template_resources_inspected')
                if type(inspected) is bool:
                    template_inspections['inspected' if inspected else 'unavailable'] += 1
                kinds = diagnostic.get('vswitch_vpc_reference_kinds')
                for kind, count in kinds.items() if isinstance(kinds, dict) else []:
                    if kind in {'literal', 'parameter', 'unresolved'} and type(count) is int and 0 <= count <= 100:
                        vpc_reference_kinds[kind] += count
                hashes = diagnostic.get('vswitch_vpc_reference_hashes')
                for value in hashes if isinstance(hashes, list) else []:
                    if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
                        vpc_reference_hashes.add(value)
                for key, allowed, target in (
                    ('vswitch_zone_reference_kinds', {'literal', 'parameter', 'unresolved'}, zone_reference_kinds),
                    ('vswitch_zone_region_categories', {'stack_region', 'other_region', 'unresolved'},
                     zone_region_categories),
                    ('vpc_presence_after_failure', {'available', 'absent', 'not_available', 'query_unavailable',
                                                   'unresolved'}, vpc_presence),
                ):
                    values = diagnostic.get(key)
                    for label, count in values.items() if isinstance(values, dict) else []:
                        if label in allowed and type(count) is int and 0 <= count <= 100:
                            target[label] += count
            region = state.get('region_id')
            if isinstance(region, str) and region:
                regions['fixture_region' if region == 'cn-hangzhou' else 'other_region'] += 1
            status = state.get("status")
            if isinstance(status, str) and status in known:
                statuses[status] += 1
            if isinstance(status, str) and status.endswith("FAILED"):
                reason = str(state.get("status_reason") or "")
                failures.update([key for key, pattern in patterns.items() if re.search(pattern, reason, re.I)]
                                or ["unknown"])
                for code in ('Forbidden.RAM', 'Forbidden.OperationDenied', 'Forbidden.ResourceAccess',
                             'Forbidden.SubUser', 'Forbidden.Unauthorized', 'Forbidden.ProductDisabled',
                             'Forbidden.CidrBlock', 'Forbidden.Ipv6', 'Forbidden.Zone',
                             'InvalidCidrBlock', 'InvalidVSwitchCidr', 'InvalidVpcCidr',
                             'InvalidParameter', 'QuotaExceeded', 'AccessDenied',
                             'Forbidden', 'Forbidden.OperateShareResource', 'PermissionDenied',
                             'Forbidden.VpcNotFound', 'InvalidVpcId.NotFound', 'InvalidZoneId.NotFound',
                             'IncorrectVSwitchStatus', 'IncorrectVpcStatus', 'TaskConflict',
                             'CreateVSwitch.IncorrectStatus.cbnStatus', 'InvalidCidrBlock.Overlapped',
                             'RouteConflict.AlreadyExist', 'QuotaExceeded.VSwitch',
                             'OperationDenied.VpcPeerExist', 'OperationDenied.CenAttached',
                             'OperationDenied.NatgwExist', 'OperationDenied.OtherSubnetCreating',
                             'OperationFailed.DistibuteLock', 'OperationDenied.ZoneIsDisabled'):
                    if re.search(r'(?<![A-Za-z0-9_.])' + re.escape(code) + r'(?![A-Za-z0-9_.])', reason):
                        codes[code] += 1
                for name in ('CidrBlock', 'VpcId', 'ZoneId', 'Ipv6CidrBlock', 'EnableIpv6', 'RamRoleName',
                             'SecurityGroupId', 'NetworkAclId', 'Ipv6', 'RAM', 'VSwitch'):
                    if re.search(r'\b' + re.escape(name) + r'\b', reason, re.I):
                        fields[name] += 1
    if not statuses and not failures:
        return {}
    facts = {"ros_stack_observed_status_counts": dict(statuses), "ros_stack_failure_categories": dict(failures),
            'ros_stack_failure_known_codes': dict(codes), 'ros_stack_failure_fields': dict(fields),
            'ros_stack_region_categories': dict(regions)}
    if template_inspections:
        facts['ros_stack_template_inspection_counts'] = dict(template_inspections)
        facts['ros_stack_vswitch_vpc_reference_kinds'] = dict(vpc_reference_kinds)
        facts['ros_stack_vswitch_vpc_reference_hashes'] = sorted(vpc_reference_hashes)[:100]
        facts['ros_stack_vswitch_zone_reference_kinds'] = dict(zone_reference_kinds)
        facts['ros_stack_vswitch_zone_region_categories'] = dict(zone_region_categories)
        facts['ros_stack_vpc_presence_after_failure'] = dict(vpc_presence)
    return facts


def _server_failure_facts(root: Path, runtime_config_dir: Path | None = None) -> dict[str, Any]:
    """Project traceback types and source locations, never exception messages."""
    kinds: Counter[str] = Counter()
    frames: set[str] = set()
    categories: Counter[str] = Counter()
    patterns = {
        "json_serialization": r"not JSON serializable|Object of type",
        "attribute_missing": r"has no attribute",
        "invalid_agent_response": r"InvalidAgentResponseError",
        "after_terminal": r"after.{0,30}terminal|already in a terminal state",
        "model_timeout": r"Provider stream idle timeout|ReadTimeout|ConnectTimeout",
        "model_rate_limit": r"RateLimitError|rate.limit|Throttling",
        "model_bad_request": r"BadRequestError|invalid_parameter_error",
        "model_connection": r"APIConnectionError|ConnectionResetError|ConnectError",
        "context_token_mismatch": r"Token.{0,100}different Context|created in a different Context",
        "invalid_path_null_byte": r"embedded null (?:byte|character)",
    }
    types = {"AttributeError", "TypeError", "ValueError", "RuntimeError", "KeyError", "AssertionError",
             "InvalidAgentResponseError", "CancelledError", "TimeoutError", "HTTPException",
             "RateLimitError", "BadRequestError", "APIConnectionError", "APITimeoutError", "InternalServerError"}
    paths = list(root.glob("server-*.*.log"))[:12]
    paths.extend(root.glob("a2a.*.log"))
    paths.extend(root.glob("agui.*.log"))
    paths.extend(_evidence_paths(root, "logs/*.log", runtime_config_dir)[:12])
    for path in sorted(set(paths)):
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for phase, status, raw_counts in re.findall(
            r"A2A natural handoff unavailable: phase=(running|resuming|paused|terminating|terminated) "
            r"status=(working|input-required|completed|failed|canceled) blocker_counts=(\{[^\n]{0,500}\})", text,
        ):
            categories["natural_handoff_phase:" + phase] += 1
            categories["natural_handoff_status:" + status] += 1
            try:
                counts = json.loads(raw_counts)
            except ValueError:
                continue
            for kind, count in counts.items() if isinstance(counts, dict) else ():
                if kind in {"execution", "agent_loop", "background_agent", "permission_cleanup",
                            "tool", "tool_batch", "llm"} and type(count) is int and 0 < count <= 10000:
                    categories["natural_handoff_blocker:" + kind] += count
        for block in text.split("Traceback (most recent call last):")[1:][-20:]:
            # Stop at the exception line; subsequent application output is not a traceback.
            lines = block.splitlines()[:160]
            for line in lines:
                match = re.search(r'File "[^"\n]*[\\/]iac_code[\\/]([A-Za-z0-9_\\/]+\.py)", line ([0-9]{1,6})', line)
                if match:
                    relative = "src/iac_code/" + match.group(1).replace("\\", "/")
                    if (Path(__file__).resolve().parents[2] / relative).is_file():
                        frames.add(relative + ":" + match.group(2))
                match = re.match(r"\s*(?:[A-Za-z_][A-Za-z_0-9]*\.)*([A-Za-z_]+):", line)
                if match and match.group(1) in types:
                    kinds[match.group(1)] += 1
                    for label, pattern in patterns.items():
                        if re.search(pattern, line, re.I):
                            categories[label] += 1
                    break
    result = {}
    if kinds:
        result["server_exception_types"] = {k: min(v, 1000) for k, v in kinds.items()}
    if frames:
        result["server_source_frames"] = sorted(frames)[:40]
    if categories:
        result["server_exception_categories"] = dict(categories)
    return result



def _provider_warning_facts(root: Path, runtime_config_dir: Path | None) -> dict[str, Any]:
    """Classify native provider failures without exporting error messages."""
    categories: Counter[str] = Counter()
    error_types: Counter[str] = Counter()
    fields: Counter[str] = Counter()
    patterns = {
        "rate_limit": r"RateLimitError|Throttling|rate.limit|status.code.{0,8}429",
        "timeout": r"APITimeoutError|ReadTimeout|ConnectTimeout|idle timeout",
        "connection": r"APIConnectionError|ConnectionResetError|ConnectError",
        "bad_request": r"BadRequestError|invalid_parameter_error|status.code.{0,8}400",
        "content_filter": r"data_inspection_failed|inappropriate content|content.filter",
        "protocol": r"UnsafeStreamProtocolError|Unsafe Qwen stream",
        "server_error": r"InternalServerError|status.code.{0,8}50[0234]",
        "parameter_range": r"InvalidParameterRange|must be (?:less|greater)|out of range|range of.{0,30}tokens",
        "authentication": r"AuthenticationError|invalid_api_key|InvalidApiKey|status.code.{0,8}401",
    }
    paths = _evidence_paths(root, "logs/*.log", runtime_config_dir)[:12]
    paths.extend(root.glob("server-*.*.log"))
    paths.extend(root.glob("a2a.*.log"))
    for path in sorted(set(paths))[:36]:
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            warning = "iac_code.providers.manager" in line and re.search(
                r"Streaming failed|Provider stream idle timeout|Unsafe Qwen stream", line)
            failure = "iac_code.services.telemetry.sink" in line and "[event] iac.api.request.failed " in line
            if not (warning or failure):
                continue
            labels = [label for label, pattern in patterns.items() if re.search(pattern, line, re.I)]
            for label in labels or ["unclassified"]:
                categories[label] += 1
            for kind in ("RateLimitError", "BadRequestError", "APIConnectionError", "APITimeoutError",
                         "InternalServerError", "AuthenticationError", "UnsafeStreamProtocolError",
                         "TimeoutError", "ValueError", "RuntimeError"):
                if re.search(r"\b" + kind + r"\b", line):
                    error_types[kind] += 1
            for field in ("max_tokens", "max_completion_tokens", "thinking_budget", "enable_thinking",
                          "reasoning_content", "tool_calls", "messages", "content"):
                if re.search(r"\b" + field + r"\b", line):
                    fields[field] += 1
    return {key: {k: min(v, 1000) for k, v in counts.items()} for key, counts in (
        ("provider_failure_categories", categories), ("provider_failure_types", error_types),
        ("provider_failure_fields", fields),
    ) if counts}

def _a2a_journal_boundary_facts(root: Path, runtime_config_dir: Path | None) -> dict[str, Any]:
    """Read native wait/terminal boundaries without exporting journal payloads."""
    boundaries = []
    for path in _evidence_paths(root, "a2a/pipeline/a2a-events.jsonl", runtime_config_dir)[:8]:
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            events = record.get("events", []) if record.get("__iac_code_record_type") == "event_group" else [record]
            if not isinstance(events, list):
                continue
            for event in events:
                kind = event.get("eventType") if isinstance(event, dict) else None
                if not isinstance(kind, str) or kind not in {
                    "input_required", "step_failed", "pipeline_failed", "pipeline_completed", "pipeline_canceled",
                }:
                    continue
                item = {"type": event["eventType"]}
                for key, allowed in {
                    "status": {"working", "waiting_input", "input_required", "completed", "failed", "canceled"},
                    "visibility": {"pending_backup", "committed"},
                }.items():
                    value = event.get(key)
                    if isinstance(value, str) and value in allowed:
                        item[key] = value
                raw_input = event.get("input")
                input_kind = raw_input.get("kind") if isinstance(raw_input, dict) else None
                if isinstance(input_kind, str) and input_kind in {
                    "candidate_selection", "deployment_confirmation", "ask_user_question",
                    "cloud_resource_selection", "pipeline_pause_confirmation",
                }:
                    item["inputKind"] = input_kind
                if kind in {"step_failed", "pipeline_failed"}:
                    data = event.get("data")
                    data = data if isinstance(data, dict) else {}
                    details = data.get("errorDetails", data.get("error_details"))
                    details = details if isinstance(details, dict) else {}
                    error_type = details.get("type")
                    if isinstance(error_type, str) and error_type in {
                        "AttributeError", "TypeError", "ValueError", "RuntimeError", "KeyError",
                        "IndexError", "AssertionError", "TimeoutError", "InvalidStateError",
                        "InvalidAgentResponseError", "PipelineStatePersistenceError",
                        "PipelineTransportDeliveryClosedError", "PermissionWaitSuspended",
                    }:
                        item["errorType"] = error_type
                    summary = data.get("errorSummary", data.get("error_summary", data.get("error")))
                    if isinstance(summary, str):
                        categories = [label for label, pattern in {
                            "unhashable_type": r"unhashable type",
                            "attribute_missing": r"has no attribute",
                            "missing_mapping_key": r"^KeyError:",
                            "index_out_of_range": r"index out of range",
                            "json_serialization": r"not JSON serializable",
                            "context_token_mismatch": r"created in a different Context",
                            "input_rejected": r"Pipeline rejected the pending input",
                            "transport_closed": r"transport.*closed|delivery.*closed",
                            "backup_failed": r"backup.*fail|snapshot.*fail",
                            "invalid_agent_response": r"InvalidAgentResponseError",
                        }.items() if re.search(pattern, summary, re.I)]
                        if categories:
                            item["errorCategories"] = categories
                        # Copy fixed identifiers only; never export exception values or payloads.
                        fields = [field for field in (
                            "candidate_index", "candidate_id", "candidate_selection", "options",
                            "user_prompt", "selected_candidate", "inputId", "toolUseId",
                        ) if re.search(r"\b" + field + r"\b", summary)]
                        if fields:
                            item["errorFields"] = fields
                    source = data.get("source")
                    if isinstance(source, str) and source in {"executor", "pipeline"}:
                        item["source"] = source
                boundaries.append(item)
                boundaries = boundaries[-12:]
    return {"native_a2a_journal_boundaries": boundaries} if boundaries else {}


def collect_live_diagnostics(
    root: Path, summary: dict[str, Any], *, runtime_config_dir: Path | None = None,
) -> dict[str, Any]:
    from scripts.a2a.debugger import _extract_pipeline_envelopes

    facts: dict[str, Any] = _server_failure_facts(root, runtime_config_dir)
    facts.update(_a2a_journal_boundary_facts(root, runtime_config_dir))
    facts.update(_pty_terminal_failure_facts(root, runtime_config_dir))
    facts.update(_provider_warning_facts(root, runtime_config_dir))
    preflight_path = root / "preflight.json"
    if preflight_path.is_file() and not preflight_path.is_symlink() and preflight_path.stat().st_size <= 1048576:
        try:
            preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            preflight = None
        if isinstance(preflight, dict):
            diagnostic: dict[str, Any] = {}
            for key in ("ok", "timedOut"):
                if isinstance(preflight.get(key), bool):
                    diagnostic[key] = preflight[key]
            code = preflight.get("returnCode")
            if type(code) is int and -65536 <= code <= 65536:
                diagnostic["returnCode"] = code
            elapsed = preflight.get("elapsedSeconds")
            if type(elapsed) in (int, float) and 0 <= elapsed <= 86400:
                diagnostic["elapsedSeconds"] = elapsed
            # Classify only fixed error tokens. Provider bodies and CLI output
            # stay on the CI host; these hints never determine acceptance.
            text = "\n".join(preflight[key] for key in ("stdout", "stderr")
                             if isinstance(preflight.get(key), str))
            categories = {
                "rate_limit": r"\bRateLimitError\b|\bThrottling\b|\brate_limit_exceeded\b",
                "authentication": r"\bAuthenticationError\b|\binvalid_api_key\b",
                "bad_request": r"\bBadRequestError\b|\binvalid_parameter_error\b",
                "connection": r"\bAPIConnectionError\b|\bConnectError\b|\bConnectionResetError\b",
                "provider_timeout": r"\bAPITimeoutError\b|\bReadTimeout\b|\bConnectTimeout\b",
                "provider_server": r"\bInternalServerError\b|\bServiceUnavailableError\b",
            }
            diagnostic["errorCategories"] = [
                category for category, pattern in categories.items() if re.search(pattern, text, re.I)
            ]
            if diagnostic:
                facts["llm_preflight"] = diagnostic
    stream_path = root / "stream-diagnostics.jsonl"
    if stream_path.is_file() and not stream_path.is_symlink() and stream_path.stat().st_size <= 65536:
        streams = []
        for line in stream_path.read_text(encoding="utf-8").splitlines()[-12:]:
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            safe = {}
            for key, allowed in {
                "outcome": {"eof", "error"},
                "error_kind": {"jsonrpc", "http", "timeout", "connection", "url", "os"},
                "last_state": {"TASK_STATE_WORKING", "TASK_STATE_SUBMITTED", "TASK_STATE_INPUT_REQUIRED",
                               "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"},
                "response_content_type": {"application/json", "text/event-stream"},
            }.items():
                if isinstance(raw.get(key), str) and raw[key] in allowed:
                    safe[key] = raw[key]
            for key in ("elapsed_seconds", "event_count", "jsonrpc_error_code"):
                value = raw.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and -1000000 <= value <= 1000000:
                    safe[key] = value
            status = raw.get("http_status")
            if type(status) is int and 100 <= status <= 599:
                safe["http_status"] = status
            if safe:
                streams.append(safe)
        if streams:
            facts["stream_diagnostics"] = streams
    abort = summary.get("abort_reason") or summary.get("error") or ""
    if isinstance(abort, str):
        wait = _known_wait(abort.rsplit("timed out waiting for ", 1)[-1])
        match = re.search(r"Timed out waiting for (.+?)(?:; last_error=| in |$)", abort, re.IGNORECASE)
        if match:
            wait = _known_wait(match.group(1)) or wait
        ended = re.search(r" ended before (.+?)(?::|$)", abort)
        if ended:
            wait = _known_wait(ended.group(1)) or wait
        if wait:
            facts["failed_wait"] = wait
        for kind in ("TimeoutError", "RuntimeError", "ValueError", "PermissionError", "ConnectionError"):
            if abort.startswith(kind + ":"):
                facts["abort_type"] = kind
        for label, marker in (
            ("selection_not_accepted", "candidate selection input was not accepted"),
            ("no_output", "no terminal output"), ("unexpected_input", "unexpected input while waiting"),
            ("wait_deadline", "timed out waiting"), ("stream_ended_before_checkpoint", " ended before "),
        ):
            if marker in abort.lower():
                facts["abort_category"] = label

    cleanup = root / "cleanup-result.json"
    if cleanup.is_file():
        try:
            value = json.loads(cleanup.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if isinstance(value, dict):
            for source, target in (
                ("resources", "cleanup_resource_count"), ("deletedStackIds", "cleanup_deleted_count"),
            ):
                if isinstance(value.get(source), list):
                    facts[target] = min(len(value[source]), 10000)
            categories: Counter[str] = Counter()
            for failure in value.get("failures", []) if isinstance(value.get("failures"), list) else []:
                text = str(failure).lower()
                category = next((label for label, marker in (
                    ("discovery_failed", "discovery failed"), ("ownership_unproven", "ownership could not be proven"),
                    ("ownership_unproven", "observed stack ownership outside exact manifest"),
                    ("delete_subprocess_failed", "cleanup subprocess exited"), ("timeout", "timeout"),
                    ("unexpected_name", "unexpected run-scoped stack outside exact ownership manifest"),
                ) if marker in text), "other")
                categories[category] += 1
            facts["cleanup_failure_categories"] = dict(categories)
            resources = value.get("resources")
            manifests = list(root.rglob("owned-stack-names.json"))
            if isinstance(resources, list) and len(manifests) == 1:
                try:
                    names = json.loads(manifests[0].read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    names = None
                if isinstance(names, list) and all(isinstance(name, str) for name in names):
                    facts["cleanup_missing_name_count"] = min(sum(
                        isinstance(item, dict) and not item.get("stackName") for item in resources
                    ), 10000)
                    facts["cleanup_unexpected_name_count"] = min(sum(
                        isinstance(item, dict) and bool(item.get("stackName")) and item["stackName"] not in names
                        for item in resources
                    ), 10000)
                    facts['cleanup_unexpected_name_same_case_count'] = min(sum(
                        isinstance(item, dict) and isinstance(item.get('stackName'), str)
                        and item['stackName'] not in names
                        and any(item['stackName'].startswith(name + '-') for name in names)
                        for item in resources
                    ), 10000)
    codes: set[str] = set()
    cleanup_attempts: list[dict[str, str]] = []
    for log in root.rglob("cleanup-*.log"):
        text = log.read_text(encoding="utf-8", errors="replace")
        codes.update(code for code in KNOWN_CODES if code in text)
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            item = value.get("cleanupDiagnostic") if isinstance(value, dict) else None
            if not isinstance(item, dict):
                continue
            projected = {}
            for key, allowed in {
                "stage": {"get_stack", "delete_stack"},
                "errorType": {"TimeoutError", "RuntimeError", "ConnectionError", "PermissionError", "SDKError"},
                "code": {*KNOWN_CODES, "unknown"},
                "status": {"CREATE_COMPLETE", "CREATE_IN_PROGRESS", "CREATE_FAILED", "DELETE_IN_PROGRESS",
                           "DELETE_FAILED", "DELETE_COMPLETE", "ROLLBACK_IN_PROGRESS", "ROLLBACK_COMPLETE", "unknown"},
            }.items():
                if isinstance(item.get(key), str) and item[key] in allowed:
                    projected[key] = item[key]
            if projected:
                cleanup_attempts.append(projected)
    if cleanup_attempts:
        facts["cleanup_attempt_diagnostics"] = cleanup_attempts[:16]
    if codes:
        facts["cleanup_known_codes"] = sorted(codes)

    counts: Counter[str] = Counter()
    stack_ids: set[str] = set()
    stack_names: set[str] = set()
    owned_names: set[str] = set()
    for filename in ("cloud-resources.json", "owned-stack-names.json", "owned-stacks.json"):
        for path in _evidence_paths(root, filename, None)[:30]:
            try:
                if path.stat().st_size > 20_000_000:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if filename != "cloud-resources.json":
                names = value.get("stackNames") if isinstance(value, dict) else None
                if isinstance(names, list):
                    owned_names.update(hashlib.sha256(n.encode()).hexdigest() for n in names if isinstance(n, str))
            elif isinstance(value, list):
                for resource in value:
                    if not isinstance(resource, dict):
                        continue
                    for key, target in (("stackId", stack_ids), ("stackName", stack_names)):
                        if isinstance(resource.get(key), str) and resource[key]:
                            target.add(hashlib.sha256(resource[key].encode()).hexdigest())
    marker_present = False
    rollback_trace: dict[tuple[str, int], dict[str, Any]] = {}
    a2a_terminal_events = []
    stack_progress_statuses: Counter[str] = Counter()
    stack_progress_last: dict[str, str] = {}
    allowed_stack_statuses = {
        "CREATE_IN_PROGRESS", "CREATE_COMPLETE", "CREATE_FAILED", "DELETE_IN_PROGRESS", "DELETE_COMPLETE",
        "DELETE_FAILED", "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE", "UPDATE_FAILED", "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CHECK_IN_PROGRESS", "CHECK_COMPLETE", "CHECK_FAILED",
    }
    rollback_steps = {
        "solution_planning_and_selection", "materialize_selected_candidate",
        "intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select", "deploying",
    }
    for path in (*root.glob("*.events.jsonl"), root / "events.jsonl", root / "repl-events.jsonl"):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                marker_present |= "candidate_step_started" in line
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("type") == "expect" and value.get("passed") is False:
                    wait = _known_wait(value.get("description"))
                    if wait:
                        facts["failed_wait"] = wait
                rpc_error = value.get("error") if isinstance(value, dict) else None
                if isinstance(rpc_error, dict):
                    code = rpc_error.get("code")
                    if type(code) is int and -32768 <= code <= -32000:
                        facts["jsonrpc_error_code"] = code
                    message = str(rpc_error.get("message") or "").casefold()
                    categories = [label for label, pattern in RPC_REQUEST_ERROR_PATTERNS.items()
                                  if re.search(pattern, message, re.I)]
                    if categories:
                        facts["jsonrpc_request_error_categories"] = categories
                    markers = [marker for marker in (
                        "resource_selection_resume_invalid", "task is already working", "active session",
                        "execution", "context", "not found", "terminal state", "permission", "rate limit",
                        "unsupported", "duplicate", "provider", "invalid params", "authentication",
                    ) if marker in message]
                    if markers:
                        facts["jsonrpc_error_markers"] = markers
                    body = json.dumps(rpc_error, ensure_ascii=False)
                    exception_types = [kind for kind in (
                        'KeyError', 'ValueError', 'AttributeError', 'TypeError', 'RuntimeError',
                        'CancelledError', 'TimeoutError', 'InvalidStateError', 'ConnectionError',
                    ) if re.search(r'\b' + kind + r'\b', body)]
                    if exception_types:
                        facts['jsonrpc_exception_types'] = exception_types
                    # File basenames are matched against a fixed source list,
                    # not copied from a private traceback.
                    sites = [name for name in (
                        'executor.py', 'jsonrpc_passthrough.py', 'task_manager.py', 'input_required.py',
                        'pipeline_bridge.py', 'runtime.py', 'session.py', 'engine.py',
                        'completion_enrichment.py', 'materialize_selected_candidate.py',
                    ) if name in body]
                    if sites:
                        facts['jsonrpc_exception_sites'] = sites
                for envelope in _extract_pipeline_envelopes(value):
                    kind = envelope.get("eventType")
                    if kind in {'step_failed', 'pipeline_failed', 'pipeline_completed'}:
                        terminal = {'type': kind}
                        payload = envelope.get('data')
                        if isinstance(payload, dict):
                            for flag, wire_key in (('failed', 'failed'), ('early_exit', 'earlyExit'),
                                                   ('user_aborted', 'userAborted')):
                                value = payload.get(wire_key, payload.get(flag))
                                if type(value) is bool:
                                    terminal[flag] = value
                        a2a_terminal_events.append(terminal)
                    sequence = envelope.get("sequence")
                    if (isinstance(kind, str)
                        and kind in {"interrupt_classified", "rollback_completed", "step_started", "input_required"}
                        and type(sequence) in {int, float} and 0 < sequence < 2**53
                        and int(sequence) == sequence and len(rollback_trace) < 64):
                        item = {"eventType": kind, "sequence": int(sequence)}
                        step = envelope.get("step")
                        step_id = step.get("id") if isinstance(step, dict) else None
                        if isinstance(step_id, str) and step_id in rollback_steps:
                            item["stepId"] = step_id
                        data = envelope.get("data")
                        if kind in {"interrupt_classified", "rollback_completed"} and isinstance(data, dict):
                            target = data.get("toStepId") or data.get("toStep") or data.get("targetStepId")
                            item["rollbackTarget"] = (
                                target if isinstance(target, str) and target in rollback_steps else "other")
                            action = data.get("action")
                            if isinstance(action, str) and action in {
                                "continue", "ignored", "supplement", "hard_interrupt",
                            }:
                                item["action"] = action
                            if kind == 'interrupt_classified':
                                reason = str(data.get('reason') or '')
                                for category, pattern in (
                                    ('judge_timeout', r'judge failed: timeout'),
                                    ('judge_parse_failure', r'(?:fallback )?parse failed:'),
                                    ('judge_failure', r'judge failed[:; ]'),
                                    ('unrelated_input', r'与当前.{0,20}无关|unrelated|irrelevant'),
                                    ('safety_rejection', r'prompt injection|注入攻击|越权'),
                                    ('goal_changed', r'需求.{0,20}(?:改变|变化|替换)|方向.{0,20}改变'),
                                ):
                                    if re.search(pattern, reason, re.I):
                                        item['reasonCategory'] = category
                                        break
                                if type(data.get('paused')) is bool:
                                    item['paused'] = data['paused']
                        rollback_trace[(kind, int(sequence))] = item
                    if kind in {"candidate_step_started", "step_started", "input_received"}:
                        counts[kind] += 1
                    data = envelope.get("data")
                    if kind == "stack_progress" and isinstance(data, dict):
                        status = data.get("status")
                        if isinstance(status, str) and status in allowed_stack_statuses:
                            stack_progress_statuses[status] += 1
                            stack_id = data.get("stackId")
                            if isinstance(stack_id, str) and stack_id:
                                stack_progress_last[hashlib.sha256(stack_id.encode()).hexdigest()] = status
                    if kind == "stack_current_changed" and isinstance(data, dict):
                        for key, target in (("stackId", stack_ids), ("stackName", stack_names)):
                            if isinstance(data.get(key), str) and data[key]:
                                target.add(hashlib.sha256(data[key].encode()).hexdigest())
                    if (
                        kind == "input_received" and isinstance(data, dict)
                        and data.get("kind") == "deployment_confirmation"
                    ):
                        action = data.get("action")
                        action = action if action in {"confirm", "cancel", "adjust", "reselect"} else "free_text"
                        counts["confirmation_" + action] += 1
                        if data.get("has_images") is True:
                            counts["confirmation_image"] += 1
    facts["a2a_event_counts"] = {key: min(value, 10000) for key, value in sorted(counts.items())}
    if stack_progress_statuses:
        facts["native_stack_progress_status_counts"] = dict(stack_progress_statuses)
        facts["native_stack_progress_last_statuses"] = dict(sorted(stack_progress_last.items())[:20])
    if a2a_terminal_events:
        facts['native_a2a_terminal_events'] = a2a_terminal_events[-8:]
    if any(item["eventType"] in {"interrupt_classified", "rollback_completed"} for item in rollback_trace.values()):
        facts["rollback_event_trace"] = sorted(rollback_trace.values(), key=lambda item: item["sequence"])
    terminal_events = []
    candidate_ui_trace = []
    repl_input_traces = []
    for path in _evidence_paths(root, "pipeline/display.jsonl", runtime_config_dir)[:12]:
        if path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        input_trace = []
        previous_confirmation = None
        for index, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") in {
                "candidate_selection_ready", "candidate_selection_submitted", "candidate_selected",
                "user_input_received", "user_input_required", "step_started", "step_completed",
            }:
                step = row.get("step_id")
                if step in rollback_steps | {"solution_planning_and_selection", "materialize_selected_candidate"}:
                    candidate_ui_trace.append({"type": row["type"], "step": step})
                    item = {'type': row['type'], 'step': step, 'index': index}
                    payload = row.get('payload')
                    if isinstance(payload, dict):
                        kind = payload.get('kind')
                        if isinstance(kind, str) and kind in {
                            'deployment_confirmation', 'candidate_selection', 'ask_user_question',
                        }:
                            item['kind'] = kind
                        if isinstance(payload.get('structured'), bool):
                            item['structured'] = payload['structured']
                        if isinstance(payload.get('action'), str) and payload['action'] in {
                            'confirm', 'adjust', 'cancel', 'reselect',
                        }:
                            item['action'] = payload['action']
                        if kind == 'deployment_confirmation' and row['type'] == 'user_input_received':
                            text = str(payload.get('selected_value') or '')
                            item['confirmation_word_present'] = bool(re.search(r'确认|\bconfirm\b', text, re.I))
                            item['adjustment_word_present'] = bool(re.search(
                                r'调整|覆盖|修改|adjust|override', text, re.I))
                        if kind == 'deployment_confirmation' and row['type'] == 'user_input_required':
                            state = (payload.get('solution_summary'), payload.get('effective_deployment_parameters'))
                            if previous_confirmation is not None:
                                item['confirmation_changed'] = state != previous_confirmation
                            previous_confirmation = state
                    input_trace.append(item)
            if not isinstance(row, dict) or row.get("type") not in {
                "step_failed", "pipeline_failed", "pipeline_completed", "pipeline_user_aborted",
            }:
                continue
            item = {"type": row["type"]}
            step = row.get("step_id")
            if step in rollback_steps | {"solution_planning_and_selection", "materialize_selected_candidate"}:
                item["step"] = step
            payload = row.get("payload")
            if isinstance(payload, dict):
                for flag in ("failed", "early_exit", "user_aborted"):
                    if isinstance(payload.get(flag), bool):
                        item[flag] = payload[flag]
                error = str(payload.get("error") or payload.get("error_summary") or payload.get("reason") or "")
                codes = [label for label, pattern in COMPLETION_ERROR_PATTERNS.items()
                         if re.search(pattern, error, re.I)]
                if codes:
                    item["reason_codes"] = codes
                details = payload.get("error_details")
                if isinstance(details, dict) and details.get("type") in {
                    "StepFailed", "RuntimeError", "ValueError", "InvalidAgentResponseError", "BadRequestError",
                }:
                    item["error_type"] = details["type"]
            terminal_events.append(item)
        if input_trace and input_trace[-24:] not in repl_input_traces:
            repl_input_traces.append(input_trace[-24:])
    if terminal_events:
        facts["native_pipeline_terminal_events"] = terminal_events[-8:]
    if candidate_ui_trace:
        facts["candidate_ui_trace"] = candidate_ui_trace[-24:]
    if repl_input_traces:
        facts['native_repl_input_traces'] = repl_input_traces
    facts["candidate_marker_without_event"] = marker_present and not counts["candidate_step_started"]
    for key, hashes in (("cloud_stack_id_hashes", stack_ids), ("cloud_stack_name_hashes", stack_names),
                        ("owned_stack_name_hashes", owned_names)):
        if hashes:
            facts[key] = sorted(hashes)[:20]

    for meta in _evidence_paths(root, "pipeline/meta.yaml", runtime_config_dir):
        try:
            state = yaml.safe_load(meta.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(state, dict):
            continue
        status = state.get("status")
        if status in {"running", "waiting_input", "completed", "failed", "canceled", "discarded"}:
            facts["pipeline_status"] = status
        handoff = state.get("normal_handoff")
        if isinstance(handoff, dict) and handoff.get("status") in {"pending", "succeeded", "failed"}:
            facts["normal_handoff_status"] = handoff["status"]
        reason = str(state.get("reason") or "")
        reason_codes = [code for code, pattern in COMPLETION_ERROR_PATTERNS.items() if re.search(pattern, reason, re.I)]
        if reason_codes:
            facts["pipeline_reason_codes"] = reason_codes
        step = state.get("current_step")
        if step in {
            "solution_planning_and_selection", "materialize_selected_candidate", "deploying", "confirm_and_select",
            "intent_parsing", "architecture_design", "architecture_detail",
            "architecture_planning", "evaluate_candidates",
        }:
            facts["pending_step"] = step
        execution = state.get("execution")
        if isinstance(execution, dict):
            kind = execution.get("pending_input_kind")
            if kind in {"ask_user_question", "candidate_selection", "deployment_confirmation"}:
                facts["pending_input_kind"] = kind
            elif not kind:
                facts["pending_input_kind"] = "none"
            question = execution.get("pending_ask_user_question_input")
            if isinstance(question, dict):
                facts["pending_question_answered"] = isinstance(question.get("answer"), dict)
    from scripts.e2e_question_driver import (
        NETWORK_DIAGNOSTIC_FILENAME,
        NETWORK_FAILURE_CATEGORIES,
        NETWORK_KNOWN_CODES,
    )

    for path in _evidence_paths(root, NETWORK_DIAGNOSTIC_FILENAME, runtime_config_dir):
        try:
            if path.stat().st_size > 4096:
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        category = value.get('network_fixture_failure_category')
        vpc_hash = value.get('network_fixture_vpc_hash')
        if value.get('network_fixture_available_before_run') is True:
            facts['network_fixture_available_before_run'] = True
        if isinstance(vpc_hash, str) and re.fullmatch(r'[0-9a-f]{64}', vpc_hash):
            facts['network_fixture_vpc_hash'] = vpc_hash
        if isinstance(category, str) and category in NETWORK_FAILURE_CATEGORIES:
            facts['network_fixture_failure_category'] = category
        stage = value.get('network_fixture_stage')
        if isinstance(stage, str) and stage in {
            'list_stacks', 'list_stack_resources', 'describe_vpcs', 'describe_zones', 'describe_vswitches',
        }:
            facts['network_fixture_stage'] = stage
        family = value.get('network_fixture_sdk_code_family')
        if isinstance(family, str) and family in {
            'Forbidden', 'AccessDenied', 'InvalidAccessKeyId', 'InvalidSecurityToken',
            'SecurityTokenExpired', 'EntityNotExist', 'NotFound', 'StackNotFound',
            'InvalidStack', 'InvalidRegion', 'InvalidParameter', 'Parameter.Invalid',
            'Throttling', 'NotSupported', 'TerraformStackNotSupported', 'InternalError', 'unknown',
        }:
            facts['network_fixture_sdk_code_family'] = family
        terms = value.get('network_fixture_sdk_code_terms')
        if isinstance(terms, list):
            facts['network_fixture_sdk_code_terms'] = sorted({
                term for term in terms if isinstance(term, str) and term in {
                'RAM', 'ResourceGroup', 'Stack', 'StackId', 'Scope', 'Permission', 'Resource',
                'Tag', 'Policy', 'Region', 'Type', 'Terraform', 'NotSupported', 'Action',
                }
            })
        for key, minimum, maximum in (
            ('network_fixture_exit_code', -255, 255), ('network_fixture_scan_retry_count', 0, 2),
            ('network_fixture_owned_stack_count', 0, 200),
        ):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and minimum <= count <= maximum:
                facts[key] = count
        code = value.get('network_fixture_scan_retry_code')
        if isinstance(code, str) and code in NETWORK_KNOWN_CODES:
            facts['network_fixture_scan_retry_code'] = code
        codes = value.get('network_fixture_known_codes')
        if isinstance(codes, list):
            facts['network_fixture_known_codes'] = sorted({
                c for c in codes if isinstance(c, str) and c in NETWORK_KNOWN_CODES})
        types = value.get('network_fixture_error_types')
        if isinstance(types, list):
            facts['network_fixture_error_types'] = sorted({kind for kind in types if isinstance(kind, str) and kind in {
                'RuntimeError', 'ValueError', 'KeyError', 'AttributeError', 'TypeError', 'ImportError',
                'ModuleNotFoundError', 'TeaException', 'ClientException', 'TimeoutError',
            }})
    facts.update(_completion_failure_facts(root, runtime_config_dir))
    facts.update(_cloud_tool_failure_facts(root, runtime_config_dir))
    facts.update(_ros_stack_failure_facts(root))
    return facts
