"""Design-level tests for completion schema separation and authoritative projection."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from iac_code.pipeline.engine.complete_step_tool import (
    CompleteStepTool,
    CompletionValidationError,
)
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_spec import A2AArtifactSpec
from iac_code.pipeline.engine.types import StepConfig, StepResult
from iac_code.services.token_counter import TokenCounter
from iac_code.tools.base import ToolContext

PIPELINE_DIR = Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling_solution_first"
TEMPLATE_PATH = "templates/0-rds.yml"
PARAMETERS = {"DBInstanceStorage": 120, "ZoneId": "cn-hangzhou-h"}
DIRECT_CONSTRAINT = {
    "id": "hc-storage",
    "target": "rds",
    "property": "storage",
    "operator": "gte",
    "value": 100,
    "unit": "GB",
    "verification_mode": "direct",
    "source": "user",
    "source_text": "数据库磁盘至少 100GB",
}
TOOL_CONSTRAINT = {**DIRECT_CONSTRAINT, "verification_mode": "tool"}


@pytest.fixture(scope="module")
def loaded():
    return load_pipeline_dir(PIPELINE_DIR)


def _step(loaded, step_id: str):
    return next(step for step in loaded.steps if step.step_id == step_id)


def _step_config(step) -> StepConfig:
    return StepConfig(
        step_id=step.step_id,
        conclusion_field=step.conclusion_field,
        forward=step.forward,
        auto_advance=step.auto_advance,
        complete_step_terminal=step.complete_step_terminal,
        max_agent_turns=step.max_agent_turns,
        conclusion_schema=step.conclusion_schema,
        completion_input_schema=step.completion_input_schema,
        completion_enricher=step.completion_enricher,
        rollback_targets=(
            ["solution_planning_and_selection"] if step.step_id == "materialize_selected_candidate" else []
        ),
        max_conclusion_retries=step.max_conclusion_retries,
        compact_completion_schema=step.config.get("compact_completion_schema") is True,
        compact_completion_errors=step.config.get("compact_completion_errors") is True,
        completion_validation_error_limit=step.config.get("completion_validation_error_limit", 1),
        conclusion_merge_context_field=step.config.get("conclusion_merge_context_field"),
        conclusion_merge_statuses=tuple(step.config.get("conclusion_merge_statuses", [])),
        hydrate_selected_candidate=step.config.get("hydrate_selected_candidate") is True,
        authoritative_candidate_context_field=step.config.get("authoritative_candidate_context_field"),
        authoritative_candidate_targets=tuple(step.config.get("authoritative_candidate_targets", [])),
        completion_record_contract=step.config.get("completion_record_contract"),
        hard_constraint_evidence_contract=step.config.get("hard_constraint_evidence_contract"),
        completion_context_paths=tuple(step.config.get("completion_context_paths", [])),
        confirmation_accepts_parameter_overrides=(
            step.config.get("confirmation_accepts_parameter_overrides") is True
        ),
    )


def _tool(
    step,
    *,
    context_snapshot: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
    cwd: Path | None = None,
    user_message: str = "",
) -> CompleteStepTool:
    records = copy.deepcopy(records or [])
    successful_tools = {
        str(record.get("tool_name"))
        for record in records
        if isinstance(record, dict) and not record.get("is_error") and record.get("tool_name")
    }
    tool_results: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("is_error"):
            continue
        result = record.get("result")
        if isinstance(result, dict):
            tool_results[str(record.get("tool_name"))] = copy.deepcopy(result)
    state = {
        "context_snapshot": copy.deepcopy(context_snapshot or {}),
        "tool_result_records": records,
        "successful_tools": successful_tools,
        "tool_results": tool_results,
        "completion_record_contract": step.config.get("completion_record_contract"),
    }
    if cwd is not None:
        state["cwd"] = str(cwd)
    return CompleteStepTool(
        _step_config(step),
        completion_guards=step.completion_guards,
        completion_guard_state=state,
        user_message=user_message,
    )


def _candidate_semantics() -> dict[str, Any]:
    return {
        "name": "RDS 方案",
        "summary": "在杭州部署一个 RDS 实例",
        "resource_intents": [{"product": "RDS", "action": "create", "role": "数据库"}],
        "topology_graph": {
            "nodes": [{"id": "rds", "label": "RDS", "product": "RDS"}],
            "edges": [],
        },
        "resource_inventory": [
            {
                "resource_id": "rds",
                "product": "RDS",
                "purpose": "数据库",
                "quantity": 1,
                "lifecycle": "create",
            }
        ],
        "rough_cost": {
            "currency": "CNY",
            "monthly_range": "¥800～¥1200/月",
            "items": [{"name": "RDS", "monthly_cost": "¥800～¥1200/月"}],
            "assumptions": ["cn-hangzhou"],
            "exclusions": [],
            "confidence": "medium",
        },
        "decision_notes": {
            "why_recommended": ["用户点名要托管数据库，RDS 直接满足"],
            "problems_solved": ["自建 MySQL 的备份与主备切换需要自己运维"],
            "pros": ["托管数据库", "自带备份与监控"],
            "cons": ["有固定费用"],
            "risks": ["规格需结合负载"],
            "tradeoffs": ["成本换运维效率"],
        },
    }


def _planning_records(
    candidates: list[dict[str, Any]],
    *,
    batch_id: str = "outline-batch-1",
    start_sequence: int = 1,
) -> list[dict[str, Any]]:
    """Project semantic test candidates into the Step 1 display-tool record contract."""

    outlines: list[dict[str, str]] = []
    for candidate in candidates:
        rough_cost = candidate.get("rough_cost") if isinstance(candidate.get("rough_cost"), dict) else {}
        notes = candidate.get("decision_notes") if isinstance(candidate.get("decision_notes"), dict) else {}
        cons = notes.get("cons") if isinstance(notes.get("cons"), list) else []
        outlines.append(
            {
                "candidate_name": str(candidate.get("name") or ""),
                "summary": str(candidate.get("summary") or ""),
                "total_monthly_cost": str(rough_cost.get("monthly_range") or ""),
                "key_tradeoff": (str(cons[0]).strip() if cons else "") or "待进一步评估",
            }
        )

    records = [
        _record(
            start_sequence,
            "show_architecture_plan",
            {"candidates": outlines},
            {"candidateSetId": batch_id, "count": len(outlines)},
            record_id=batch_id,
            region=None,
        )
    ]
    for index, candidate in enumerate(candidates):
        rough_cost = candidate.get("rough_cost") if isinstance(candidate.get("rough_cost"), dict) else {}
        inventory = copy.deepcopy(candidate.get("resource_inventory") or [])
        records.append(
            _record(
                start_sequence + index + 1,
                "show_candidate_detail",
                {
                    "candidate_index": index,
                    "candidate_name": str(candidate.get("name") or ""),
                    "applicable_scenarios": copy.deepcopy(candidate.get("applicable_scenarios") or []),
                    "resource_intents": copy.deepcopy(candidate.get("resource_intents") or []),
                    "topology_graph": copy.deepcopy(candidate.get("topology_graph") or {}),
                    "resource_inventory": inventory,
                    "cost_assumptions": copy.deepcopy(rough_cost.get("assumptions") or []),
                    "cost_exclusions": copy.deepcopy(rough_cost.get("exclusions") or []),
                    "cost_confidence": rough_cost.get("confidence"),
                    "decision_notes": copy.deepcopy(candidate.get("decision_notes") or {}),
                },
                {"candidateSetId": batch_id, "candidateIndex": index},
                record_id=f"{batch_id}-detail-{index}",
                region=None,
            )
        )
    return records


def _awaiting_selection_delta() -> dict[str, Any]:
    return {
        "conclusion": {
            "status": "awaiting_selection",
            "intent": {
                "cloud_platform": "aliyun",
                "resource_intents": [{"product": "RDS", "action": "create", "source": "user"}],
                "hard_constraints": [],
            },
        }
    }


def _selection(*, constraint: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = {
        **_candidate_semantics(),
        "candidate_id": "candidate-0",
        "output_path": TEMPLATE_PATH,
        "products": ["RDS"],
        "topology": "RDS",
        "hard_constraints": [copy.deepcopy(constraint or DIRECT_CONSTRAINT)],
        "why_recommended": ["用户点名要托管数据库，RDS 直接满足"],
        "problems_solved": ["自建 MySQL 的备份与主备切换需要自己运维"],
        "pros": ["托管数据库", "自带备份与监控"],
        "cons": ["有固定费用"],
        "risks": ["规格需结合负载"],
        "tradeoffs": ["成本换运维效率"],
    }
    selected.pop("decision_notes", None)
    return {
        "status": "selected",
        "continue_pipeline": True,
        "is_infra_intent": True,
        "intent": {
            "cloud_platform": "aliyun",
            "hard_constraints": [copy.deepcopy(constraint or DIRECT_CONSTRAINT)],
        },
        "candidates": [copy.deepcopy(selected)],
        "options": [{"name": selected["name"], "candidate_index": 0}],
        "selected_candidate_index": 0,
        "selected_candidate_name": selected["name"],
        "selected_candidate": copy.deepcopy(selected),
    }


def _write_template(cwd: Path) -> None:
    path = cwd / TEMPLATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  DBInstanceStorage:
    Type: Number
    Default: 120
  ZoneId:
    Type: String
Resources: {}
""",
        encoding="utf-8",
    )


def _write_template_with_constraints(cwd: Path) -> None:
    path = cwd / TEMPLATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  DBInstanceStorage:
    Type: Number
    Default: 120
    MinValue: 100
    MaxValue: 500
  ZoneId:
    Type: String
    AllowedValues:
      - cn-hangzhou-h
      - cn-hangzhou-k
    ConstraintDescription: 只能选择杭州 h/k 可用区
Resources: {}
""",
        encoding="utf-8",
    )


def _write_template_with_intrinsic(cwd: Path) -> None:
    path = cwd / TEMPLATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  DBInstanceStorage:
    Type: Number
    Default: 120
  ZoneId:
    Type: String
Resources:
  Database:
    Type: ALIYUN::RDS::DBInstance
    Properties:
      ZoneId: !Ref ZoneId
      DBInstanceStorage: !Ref DBInstanceStorage
""",
        encoding="utf-8",
    )


def _record(
    sequence: int,
    tool_name: str,
    tool_input: dict[str, Any],
    result: dict[str, Any],
    *,
    record_id: str | None = None,
    is_error: bool = False,
    region: str | None = "cn-hangzhou",
    error_summary: str = "",
) -> dict[str, Any]:
    record = {
        "record_id": record_id or f"tool-{sequence}",
        "sequence": sequence,
        "tool_name": tool_name,
        "input": copy.deepcopy(tool_input),
        "result": copy.deepcopy(result),
        "is_error": is_error,
        "error_summary": error_summary,
    }
    if region:
        record["effective_region_id"] = region
    return record


def _records(
    *,
    parameters: dict[str, Any] | None = None,
    preview_parameters: dict[str, Any] | None = None,
    quote_result: dict[str, Any] | None = None,
    quote_error: bool = False,
    constraint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    parameters = copy.deepcopy(parameters or PARAMETERS)
    preview_parameters = copy.deepcopy(preview_parameters if preview_parameters is not None else parameters)
    quote_result = copy.deepcopy(
        quote_result
        if quote_result is not None
        else {
            "OriginalAmount": "1280",
            "TradeAmount": "1024",
            "Currency": "CNY",
            "Resources": [
                {
                    "ResourceType": "ALIYUN::RDS::DBInstance",
                    "Spec": "mysql.n2.medium.1 x 1",
                    "OriginalAmount": "1280",
                    "TradeAmount": "1024",
                }
            ],
        }
    )
    return [
        _record(1, "write_file", {"path": TEMPLATE_PATH}, {"file_path": TEMPLATE_PATH}, region=None),
        _record(
            2,
            "ros_validate_template",
            {"template_url": TEMPLATE_PATH, "region_id": "cn-hangzhou"},
            {"Parameters": {}},
        ),
        _record(
            3,
            "ros_preview_template",
            {
                "template_url": TEMPLATE_PATH,
                "region_id": "cn-hangzhou",
                "stack_name": "preview-rds",
                "parameters": preview_parameters,
            },
            {"Stack": {"Resources": []}},
        ),
        _record(
            4,
            "aliyun_api",
            {"product": "rds", "action": "DescribeDBInstanceAttribute"},
            {"Items": [{"Storage": 120}]},
            record_id="tool-evidence",
        ),
        _record(
            5,
            "ros_estimate_template_cost",
            {
                "template_url": TEMPLATE_PATH,
                "region_id": "cn-hangzhou",
                "parameters": parameters,
            },
            quote_result,
            is_error=quote_error,
            error_summary="pricing failed" if quote_error else "",
        ),
    ]


def _check(*, evidence_type: str = "template") -> dict[str, Any]:
    if evidence_type == "template":
        evidence = [{"type": "template", "parameter_name": "DBInstanceStorage"}]
    elif evidence_type == "context":
        evidence = [{"type": "context", "context_path": "solution_selection.intent.storage"}]
    else:
        evidence = [
            {
                "type": "tool",
                "record_id": "tool-evidence",
                "tool_name": "aliyun_api",
                "result_path": "Items.0.Storage",
            }
        ]
    return {
        "constraint_id": "hc-storage",
        "status": "satisfied",
        "actual_value": 120,
        "actual_unit": "GB",
        "parameter_values": {"DBInstanceStorage": 120},
        "evidence": evidence,
    }


def _waiting_delta(
    *,
    check: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    missing: list[dict[str, Any]] | None = None,
):
    return {
        "conclusion": {
            "status": "awaiting_confirmation",
            "solution_summary": "在杭州按最终参数部署一个 RDS，精确询价为列表价 ¥1,280/月。",
            "parameter_overrides": copy.deepcopy(overrides or {}),
            "missing_deployment_parameters": copy.deepcopy(missing or []),
            "hard_constraint_checks": [copy.deepcopy(check or _check())],
        }
    }


def _finalize(tool: CompleteStepTool, payload: dict[str, Any]) -> StepResult:
    finalized = tool.finalize_completion_input(copy.deepcopy(payload))
    assert isinstance(finalized, StepResult), (
        finalized.message if isinstance(finalized, CompletionValidationError) else finalized
    )
    return finalized


def _assert_error(tool: CompleteStepTool, payload: dict[str, Any], text: str) -> CompletionValidationError:
    finalized = tool.finalize_completion_input(copy.deepcopy(payload))
    assert isinstance(finalized, CompletionValidationError)
    assert text in finalized.message
    return finalized


def _contains_annotation(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in ("description", "title", "examples")) or any(
            _contains_annotation(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_annotation(child) for child in value)
    return False


class TestSchemaSeparation:
    def test_model_schema_keeps_nested_structure_without_annotations_or_runtime_fields(self, loaded):
        step1 = _step(loaded, "solution_planning_and_selection")
        step2 = _step(loaded, "materialize_selected_candidate")
        schema1 = _tool(step1).input_schema
        schema2 = _tool(step2).input_schema

        assert not _contains_annotation(schema1)
        assert not _contains_annotation(schema2)
        planning_fields = schema1["properties"]["conclusion"]["properties"]
        assert "candidates" not in planning_fields
        assert set(planning_fields) == {
            "status",
            "intent",
            "selected_candidate_index",
            "rejection_reason",
        }
        step2_fields = schema2["properties"]["conclusion"]["properties"]
        assert "selected_candidate_result" not in step2_fields
        assert "template_url" not in step2_fields
        assert "confirmation" not in step2_fields
        evidence = step2_fields["hard_constraint_checks"]["items"]["properties"]["evidence"]["items"]
        assert len(evidence["oneOf"]) == 3

    def test_final_model_tool_schemas_stay_within_measured_token_budgets(self, loaded):
        counter = TokenCounter(model="deepseek-v4-flash-0731")
        counts = {
            step.step_id: counter.count_tool_definition(_tool(step))
            for step in loaded.steps
        }

        # Step 1 complete_step 只提交步骤语义；候选详情由展示工具记录承载。
        assert counts["solution_planning_and_selection"] <= 400
        assert counts["materialize_selected_candidate"] <= 700
        assert counts["deploying"] <= 200

    def test_raw_error_is_path_aware_local_and_uses_input_description(self, loaded):
        step = _step(loaded, "materialize_selected_candidate")
        tool = _tool(step)
        payload = _waiting_delta()
        payload["conclusion"]["hard_constraint_checks"][0]["evidence"] = [
            {"type": "tool", "record_id": 123, "result_path": "Items.0.Storage"}
        ]

        valid, message = tool.validate_input(payload)
        diagnostic = json.loads(message)

        assert valid is False
        assert diagnostic["error"] == "completion_input_schema_validation_failed"
        assert diagnostic["path"].startswith("/hard_constraint_checks/0/evidence/0")
        assert diagnostic["received"] != payload
        assert len(message) < 1400
        assert "selected_candidate_result" not in message
        assert "Step 2 的完整物化与确认结论" not in message

    def test_raw_error_bounds_invalid_string_in_message_and_received(self, loaded):
        step = _step(loaded, "materialize_selected_candidate")
        payload = {"conclusion": {"status": "x" * 5000}}

        valid, message = _tool(step).validate_input(payload)
        diagnostic = json.loads(message)

        assert valid is False
        assert len(message) < 1400
        assert "x" * 500 not in message
        assert diagnostic["message"] == "value is not one of the allowed values"
        assert diagnostic["received"].endswith("…")

    def test_raw_error_explains_that_conclusion_fields_cannot_be_top_level(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        payload = {
            "conclusion": {"status": "awaiting_selection", "intent": {}},
            "candidates": [],
        }

        valid, message = _tool(step).validate_input(payload)
        diagnostic = json.loads(message)
        details = diagnostic.get("errors", [diagnostic])
        top_level = next(item for item in details if item["path"] == "")

        assert valid is False
        assert top_level["validator"] == "additionalProperties"
        assert "inside conclusion" in top_level["description"]

    def test_raw_error_returns_at_most_five_field_diagnostics(self):
        field_names = [f"field_{index}" for index in range(6)]
        tool = CompleteStepTool(
            StepConfig(
                step_id="projection",
                conclusion_field="projection",
                forward=None,
                completion_input_schema={
                    "type": "object",
                    "required": field_names,
                    "properties": {
                        name: {"type": "string", "description": f"Description for {name}."}
                        for name in field_names
                    },
                    "additionalProperties": False,
                },
                completion_validation_error_limit=5,
            )
        )

        valid, message = tool.validate_input({"conclusion": {}})
        diagnostic = json.loads(message)

        assert valid is False
        assert diagnostic["error"] == "completion_input_schema_validation_failed"
        assert diagnostic["returnedErrorCount"] == 5
        assert diagnostic["truncated"] is True
        assert len(diagnostic["errors"]) == 5
        assert [item["path"] for item in diagnostic["errors"]] == [f"/{name}" for name in field_names[:5]]
        assert [item["description"] for item in diagnostic["errors"]] == [
            f"Description for {name}." for name in field_names[:5]
        ]

    @pytest.mark.asyncio
    async def test_raw_and_runtime_failures_share_the_same_retry_budget(self):
        tool = CompleteStepTool(
            StepConfig(
                step_id="projection",
                conclusion_field="projection",
                forward=None,
                conclusion_schema={
                    "type": "object",
                    "required": ["status", "python_field"],
                    "properties": {"status": {"const": "done"}, "python_field": {"type": "string"}},
                },
                completion_input_schema={
                    "type": "object",
                    "required": ["status"],
                    "additionalProperties": False,
                    "properties": {"status": {"const": "done"}},
                },
                max_conclusion_retries=1,
            )
        )

        valid, _ = tool.validate_input({"conclusion": {"status": "wrong"}})
        first_error = tool.validation_error_result({"conclusion": {"status": "wrong"}})
        assert valid is False
        assert first_error is not None and first_error.is_error is True
        assert "step_result" not in (first_error.metadata or {})

        valid, _ = tool.validate_input({"conclusion": {"status": "done"}})
        terminal = await tool.execute(
            tool_input={"conclusion": {"status": "done"}},
            context=ToolContext(),
        )
        assert valid is True
        assert terminal.is_error is True
        assert terminal.metadata["step_result"].status.value == "failed"


class TestStepOneProjection:
    def test_awaiting_and_selected_deltas_expand_to_authoritative_candidates(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        records = _planning_records([_candidate_semantics()])
        awaiting = _finalize(
            _tool(step, records=records, user_message="部署 RDS"),
            _awaiting_selection_delta(),
        )

        candidate = awaiting.conclusion["candidates"][0]
        assert candidate["candidate_id"] == "candidate-0"
        assert candidate["output_path"] == "templates/0-rds.yml"
        assert candidate["products"] == ["RDS"]
        assert candidate["pros"] == ["托管数据库", "自带备份与监控"]
        assert candidate["why_recommended"] == ["用户点名要托管数据库，RDS 直接满足"]
        assert candidate["problems_solved"] == ["自建 MySQL 的备份与主备切换需要自己运维"]
        assert "decision_notes" not in candidate
        assert awaiting.conclusion["options"][0]["candidate_index"] == 0
        assert awaiting.conclusion["candidate_set_id"] == "outline-batch-1"

        selected = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": awaiting.conclusion},
                user_message='{"candidate_index":0}',
            ),
            {"conclusion": {"status": "selected", "selected_candidate_index": 0}},
        )
        assert selected.conclusion["selected_candidate"] == selected.conclusion["candidates"][0]
        assert selected.conclusion["selected_candidate_name"] == "RDS 方案"
        assert selected.conclusion["user_input"] == '{"candidate_index":0}'

    def test_candidate_details_must_preserve_explicit_forbidden_resource_intent(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        payload = _awaiting_selection_delta()
        payload["conclusion"]["intent"]["resource_intents"].append(
            {"product": "ECS", "action": "forbid", "source": "user"}
        )

        error = _assert_error(
            _tool(step, records=_planning_records([_candidate_semantics()])),
            payload,
            "ECS:forbid",
        )

        assert "corrected candidate batch and details" in error.message

    def test_replanning_does_not_repeat_the_price_and_tradeoff_in_the_option_summary(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")

        def project(candidate: dict[str, Any]) -> str:
            result = _finalize(
                _tool(step, records=_planning_records([candidate]), user_message="部署 RDS"),
                _awaiting_selection_delta(),
            )
            return result.conclusion["options"][0]["summary"]

        composed = project(_candidate_semantics())
        assert composed == "在杭州部署一个 RDS 实例；¥800～¥1200/月；有固定费用"

        # 重新规划那一轮，模型在自己的上下文里看到的是上一轮拼好的选项文案，
        # 会把它原样当成候选概述交回来（真实录制就是这样），拼接必须幂等。
        echoed = _candidate_semantics()
        echoed["summary"] = composed
        assert project(echoed) == composed

    def test_latest_outline_batch_atomically_replaces_old_candidate_count_and_details(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")

        def candidate(name: str) -> dict[str, Any]:
            value = _candidate_semantics()
            value["name"] = name
            return value

        old = [candidate("单机 ECS 起步方案"), candidate("弹性高可用方案")]
        latest = [candidate("轻量应用服务器方案"), *old]
        records = _planning_records(old, batch_id="old-batch", start_sequence=1)
        records.extend(_planning_records(latest, batch_id="new-batch", start_sequence=10))
        corrected = _finalize(_tool(step, records=records), _awaiting_selection_delta())

        assert [option["name"] for option in corrected.conclusion["options"]] == [item["name"] for item in latest]
        assert corrected.conclusion["candidate_set_id"] == "new-batch"

    def test_new_batch_must_return_to_awaiting_selection_before_it_can_be_selected(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        old = _finalize(
            _tool(step, records=_planning_records([_candidate_semantics()], batch_id="old-batch")),
            _awaiting_selection_delta(),
        ).conclusion
        new_candidate = {**_candidate_semantics(), "name": "新的 RDS 方案"}

        error = _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": old},
                records=_planning_records([new_candidate], batch_id="new-batch"),
            ),
            {"conclusion": {"status": "selected", "selected_candidate_index": 0}},
            "new candidate batch",
        )

        assert "status awaiting_selection" in error.message

    def test_complete_step_blocks_when_current_batch_detail_is_missing(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidates = [_candidate_semantics(), {**_candidate_semantics(), "name": "RDS 高可用方案"}]
        records = _planning_records(candidates)
        records.pop()

        error = _assert_error(_tool(step, records=records), _awaiting_selection_delta(), "candidate 1")
        assert "missing show_candidate_detail" in error.message
        assert error.phase == "enrichment"

    def test_latest_failed_detail_invalidates_earlier_success(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        records = _planning_records([_candidate_semantics()])
        failed_input = copy.deepcopy(records[-1]["input"])
        records.append(
            _record(
                3,
                "show_candidate_detail",
                failed_input,
                {},
                is_error=True,
                error_summary="topology_graph is invalid",
                region=None,
            )
        )

        error = _assert_error(_tool(step, records=records), _awaiting_selection_delta(), "detail failed")
        assert "topology_graph is invalid" in error.message

    def test_failed_out_of_range_detail_does_not_poison_a_complete_batch(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidates = [_candidate_semantics(), {**_candidate_semantics(), "name": "RDS 高可用方案"}]
        records = _planning_records(candidates)
        records.append(
            _record(
                4,
                "show_candidate_detail",
                {"candidate_index": 2, "candidate_name": "不存在的方案"},
                {},
                is_error=True,
                region=None,
                error_summary="expected candidate_index=0",
            )
        )

        result = _finalize(_tool(step, records=records), _awaiting_selection_delta())

        assert [candidate["name"] for candidate in result.conclusion["candidates"]] == [
            "RDS 方案",
            "RDS 高可用方案",
        ]

    def test_detail_explicitly_bound_to_old_batch_is_not_projected_into_new_batch(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidate = _candidate_semantics()
        records = _planning_records([candidate], batch_id="old-batch")
        records.append(_planning_records([candidate], batch_id="new-batch", start_sequence=10)[0])
        stale_detail = copy.deepcopy(records[1])
        stale_detail.update(
            {
                "record_id": "stale-detail-after-new-outline",
                "sequence": 11,
                "candidate_set_id": "old-batch",
            }
        )
        records.append(stale_detail)

        error = _assert_error(_tool(step, records=records), _awaiting_selection_delta(), "candidate 0")

        assert "missing show_candidate_detail" in error.message

    def test_completion_error_reports_at_most_five_batch_problems(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidates = [{**_candidate_semantics(), "name": f"方案 {index}"} for index in range(3)]
        records = _planning_records(candidates)[:1]
        for index in range(3, 6):
            records.append(
                _record(
                    index + 2,
                    "show_candidate_detail",
                    {"candidate_index": index, "candidate_name": f"越界方案 {index}"},
                    {},
                    region=None,
                )
            )

        error = _assert_error(_tool(step, records=records), _awaiting_selection_delta(), "fully detailed")
        assert "1 more error(s) omitted" in error.message

    def test_option_summary_keeps_semicolon_prose_and_replaces_old_price_tail(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidate = _candidate_semantics()
        base = "复用现有网络；减少资源数量；适合测试环境"
        candidate["summary"] = f"{base}；¥80～¥300/月；旧代价"
        candidate["rough_cost"]["monthly_range"] = "¥100～¥360/月"
        candidate["decision_notes"]["cons"] = ["新代价"]

        summary = _finalize(
            _tool(step, records=_planning_records([candidate]), user_message="部署 RDS"),
            _awaiting_selection_delta(),
        ).conclusion["options"][0]["summary"]

        assert summary == f"{base}；¥100～¥360/月；新代价"

    def test_status_only_awaiting_delta_reopens_saved_candidates_but_cannot_create_them(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        awaiting = _finalize(
            _tool(step, records=_planning_records([_candidate_semantics()]), user_message="部署 RDS"),
            _awaiting_selection_delta(),
        )
        reopen_delta = {"conclusion": {"status": "awaiting_selection"}}

        valid, _message = _tool(step, context_snapshot={"solution_selection": awaiting.conclusion}).validate_input(
            reopen_delta
        )
        assert valid is True
        reopened = _finalize(
            _tool(step, context_snapshot={"solution_selection": awaiting.conclusion}, user_message="重新选择方案"),
            reopen_delta,
        )
        assert reopened.conclusion == awaiting.conclusion

        _assert_error(_tool(step), reopen_delta, "structured intent")

    def test_model_cannot_submit_candidates_to_complete_step(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        payload = _awaiting_selection_delta()
        payload["conclusion"]["candidates"] = [_candidate_semantics()]
        valid, message = _tool(step).validate_input(payload)

        assert valid is False
        assert "candidates" in message

    @pytest.mark.parametrize("field", ["why_recommended", "problems_solved", "pros", "cons"])
    def test_invalid_persuasion_in_detail_record_blocks_completion(self, loaded, field):
        step = _step(loaded, "solution_planning_and_selection")
        candidate = _candidate_semantics()
        candidate["decision_notes"][field] = ["   ", "\t"]
        error = _assert_error(
            _tool(step, records=_planning_records([candidate]), user_message="部署 RDS"),
            _awaiting_selection_delta(),
            f"candidates[0].decision_notes.{field}",
        )
        assert error.phase == "enrichment"

    def test_persuasion_entries_are_trimmed_and_survive_reopen_as_flat_fields(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        candidate = _candidate_semantics()
        candidate["decision_notes"]["why_recommended"] = ["  用户点名要托管数据库  ", "   "]

        awaiting = _finalize(
            _tool(step, records=_planning_records([candidate]), user_message="部署 RDS"),
            _awaiting_selection_delta(),
        )
        assert awaiting.conclusion["candidates"][0]["why_recommended"] == ["用户点名要托管数据库"]

        reopened = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": awaiting.conclusion},
                user_message="重新选择方案",
            ),
            {"conclusion": {"status": "awaiting_selection"}},
        )
        assert reopened.conclusion["candidates"][0]["why_recommended"] == ["用户点名要托管数据库"]
        assert reopened.conclusion["candidates"][0]["problems_solved"] == awaiting.conclusion["candidates"][0][
            "problems_solved"
        ]

    @pytest.mark.asyncio
    async def test_execute_preserves_submitted_delta_and_returns_normalized_result(self, loaded):
        step = _step(loaded, "solution_planning_and_selection")
        payload = _awaiting_selection_delta()
        original = copy.deepcopy(payload)

        result = await _tool(
            step,
            records=_planning_records([_candidate_semantics()]),
            user_message="部署 RDS",
        ).execute(
            tool_input=payload,
            context=ToolContext(),
        )

        assert result.is_error is False
        assert result.metadata["submitted_delta"] == original
        assert payload == original
        assert result.metadata["step_result"].conclusion["candidates"][0]["candidate_id"] == "candidate-0"


class TestStepTwoProjection:
    def test_semantic_delta_maps_to_canonical_runtime_shape(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )
        conclusion = result.conclusion
        cost = conclusion["selected_candidate_result"]["cost"]

        assert conclusion["template_url"] == TEMPLATE_PATH
        assert conclusion["effective_deployment_parameters"] == PARAMETERS
        assert cost["deployment_parameters"] == PARAMETERS
        assert cost["quote_status"] == "succeeded"
        assert cost["monthly_estimate"] == (
            "¥1,280.00/month (list price; about ¥1,024.00/month after contract discount)"
        )
        assert cost["resources"] == [
            {
                "type": "DBInstance",
                "spec": "mysql.n2.medium.1 x 1",
                "cost": "¥1,280.00/month (list price; about ¥1,024.00/month after contract discount)",
            }
        ]
        assert cost["preview_validation"]["succeeded"] is True
        assert conclusion["preview_ready_for_create"] is True
        assert "selected_candidate" not in conclusion
        assert "template" not in conclusion["selected_candidate_result"]["template"]
        assert "confirmation" not in conclusion

    def test_status_only_structured_confirm_rebuilds_same_authoritative_facts(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(),
        ).conclusion

        confirmed = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                records=records,
                cwd=tmp_path,
                user_message='{"action":"confirm"}',
            ),
            {"conclusion": {"status": "confirmed"}},
        ).conclusion

        assert confirmed["status"] == "confirmed"
        assert confirmed["deployment_confirmed"] is True
        assert confirmed["template_url"] == waiting["template_url"]
        assert confirmed["effective_deployment_parameters"] == waiting["effective_deployment_parameters"]
        assert confirmed["selected_candidate_result"] == waiting["selected_candidate_result"]
        assert confirmed["confirmation"] == {
            "action": "confirm",
            "input_type": "structured",
            "user_input": '{"action":"confirm"}',
            "parameter_overrides": {},
        }
        assert "user_prompt" not in confirmed
        assert "options" not in confirmed

    def test_structured_confirm_with_new_parameters_merges_in_one_shot(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(),
        ).conclusion
        confirm_message = json.dumps(
            {"action": "confirm", "parameter_overrides": {"DBInstanceStorage": 200}}, ensure_ascii=False
        )

        confirmed = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                # The very same tool history the quote already used: no regeneration, no new Preview and
                # no new ROS pricing call is needed to resolve this confirmation.
                records=records,
                cwd=tmp_path,
                user_message=confirm_message,
            ),
            {"conclusion": {"status": "confirmed"}},
        ).conclusion

        assert confirmed["status"] == "confirmed"
        assert confirmed["deployment_confirmed"] is True
        assert confirmed["effective_deployment_parameters"] == {**PARAMETERS, "DBInstanceStorage": 200}
        assert confirmed["parameter_overrides"] == {"DBInstanceStorage": 200}
        assert confirmed["confirmation"] == {
            "action": "confirm",
            "input_type": "structured",
            "user_input": confirm_message,
            "parameter_overrides": {"DBInstanceStorage": 200},
        }
        assert confirmed["preview_ready_for_create"] is False
        assert confirmed["template_url"] == waiting["template_url"] == TEMPLATE_PATH
        assert confirmed["selected_candidate_result"]["template"]["file_path"] == TEMPLATE_PATH
        cost = confirmed["selected_candidate_result"]["cost"]
        assert cost["preview_validation"]["succeeded"] is False
        assert cost["deployment_parameters"] == PARAMETERS
        assert cost["monthly_estimate"] == waiting["selected_candidate_result"]["cost"]["monthly_estimate"]
        assert "user_prompt" not in confirmed
        assert "options" not in confirmed

    def test_confirmed_overrides_accumulate_onto_previously_saved_overrides(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(overrides={"ZoneId": "cn-hangzhou-h"}),
        ).conclusion
        assert waiting["parameter_overrides"] == {"ZoneId": "cn-hangzhou-h"}
        confirm_message = json.dumps(
            {"action": "confirm", "parameter_overrides": {"DBInstanceStorage": 200}}, ensure_ascii=False
        )

        confirmed = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                records=records,
                cwd=tmp_path,
                user_message=confirm_message,
            ),
            {"conclusion": {"status": "confirmed"}},
        ).conclusion

        assert confirmed["parameter_overrides"] == {"ZoneId": "cn-hangzhou-h", "DBInstanceStorage": 200}
        assert confirmed["effective_deployment_parameters"] == {**PARAMETERS, "DBInstanceStorage": 200}
        assert confirmed["confirmation"]["parameter_overrides"] == {"DBInstanceStorage": 200}

    def test_one_shot_confirm_closes_the_user_required_gap_it_supplies(self, loaded, tmp_path):
        # 上一轮询价报出的 user_required 缺口，正是用户在这次确认里填上的值。缺口已被本次提交关闭，
        # 不能再按旧询价原样继承下来触发「confirmed 不得含 user_required 缺口」的守卫，
        # 否则这次确定性确认会被打回 LLM 恢复轮，用户就要确认两次。
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(
                missing=[{"name": "ZoneId", "reason": "只能由用户选择可用区", "classification": "user_required"}]
            ),
        ).conclusion
        waiting_cost = waiting["selected_candidate_result"]["cost"]
        assert [item["name"] for item in waiting_cost["user_required_missing_parameters"]] == ["ZoneId"]
        confirm_message = json.dumps(
            {"action": "confirm", "parameter_overrides": {"ZoneId": "cn-hangzhou-k"}}, ensure_ascii=False
        )

        confirmed = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                records=records,
                cwd=tmp_path,
                user_message=confirm_message,
            ),
            {"conclusion": {"status": "confirmed"}},
        ).conclusion

        assert confirmed["status"] == "confirmed"
        assert confirmed["deployment_confirmed"] is True
        assert confirmed["effective_deployment_parameters"] == {**PARAMETERS, "ZoneId": "cn-hangzhou-k"}
        assert confirmed["parameter_overrides"] == {"ZoneId": "cn-hangzhou-k"}
        cost = confirmed["selected_candidate_result"]["cost"]
        assert cost["missing_deployment_parameters"] == []
        assert cost["user_required_missing_parameters"] == []
        # 参数变了：Step 3 走常规部署校验路径，不复用旧 Preview。
        assert confirmed["preview_ready_for_create"] is False

    def test_one_shot_confirm_still_blocks_on_a_gap_it_does_not_supply(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(
                missing=[
                    {"name": "ZoneId", "reason": "只能由用户选择可用区", "classification": "user_required"},
                    {"name": "DBInstanceStorage", "reason": "只能由用户确认容量", "classification": "user_required"},
                ]
            ),
        ).conclusion
        confirm_message = json.dumps(
            {"action": "confirm", "parameter_overrides": {"ZoneId": "cn-hangzhou-k"}}, ensure_ascii=False
        )

        _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                records=records,
                cwd=tmp_path,
                user_message=confirm_message,
            ),
            {"conclusion": {"status": "confirmed"}},
            "user-required parameter gaps",
        )

    @pytest.mark.parametrize(
        ("overrides", "text", "constrained_template"),
        [
            ({"NotDeclared": 1}, "is not declared in template Parameters", False),
            ({"DBInstanceStorage": "很大"}, "must match the declared template type Number", False),
            ({"ZoneId": ""}, "is required and cannot be empty", False),
            ({"DBInstanceStorage": 50}, "is below the template MinValue", True),
            ({"DBInstanceStorage": 900}, "exceeds the template MaxValue", True),
            ({"ZoneId": "cn-beijing-a"}, "is outside the template AllowedValues", True),
        ],
    )
    def test_illegal_confirmed_parameters_are_specific_local_errors(
        self,
        loaded,
        tmp_path,
        overrides,
        text,
        constrained_template,
    ):
        if constrained_template:
            _write_template_with_constraints(tmp_path)
        else:
            _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(),
        ).conclusion
        confirm_message = json.dumps({"action": "confirm", "parameter_overrides": overrides}, ensure_ascii=False)

        error = _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "selected_plan": waiting},
                records=records,
                cwd=tmp_path,
                user_message=confirm_message,
            ),
            {"conclusion": {"status": "confirmed"}},
            text,
        )
        for value in overrides.values():
            if isinstance(value, str) and value:
                assert value not in error.message

    def test_illegal_parameters_are_rejected_before_the_step_leaves_waiting_input(self, loaded, tmp_path):
        _write_template_with_constraints(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        assert step.validate_structured_confirmation is not None
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        ).conclusion

        rejected = step.validate_structured_confirmation(
            conclusion=waiting,
            user_message=json.dumps({"action": "confirm", "parameter_overrides": {"ZoneId": "cn-beijing-a"}}),
            cwd=str(tmp_path),
            config=step.config,
        )
        accepted = step.validate_structured_confirmation(
            conclusion=waiting,
            user_message=json.dumps({"action": "confirm", "parameter_overrides": {"DBInstanceStorage": 200}}),
            cwd=str(tmp_path),
            config=step.config,
        )
        without_overrides = step.validate_structured_confirmation(
            conclusion=waiting,
            user_message='{"action":"confirm"}',
            cwd=str(tmp_path),
            config=step.config,
        )
        natural_language = step.validate_structured_confirmation(
            conclusion=waiting,
            user_message="把磁盘调到 200GB",
            cwd=str(tmp_path),
            config=step.config,
        )

        assert isinstance(rejected, str) and "AllowedValues" in rejected
        assert "只能选择杭州 h/k 可用区" in rejected
        assert accepted is None
        assert without_overrides is None
        assert natural_language is None

    def test_adjust_payload_cannot_be_resolved_as_a_confirmation(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        records = _records()
        waiting = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(),
        ).conclusion

        finalized = _tool(
            step,
            context_snapshot={"solution_selection": selection, "selected_plan": waiting},
            records=records,
            cwd=tmp_path,
            user_message=json.dumps({"action": "adjust", "parameter_overrides": {"DBInstanceStorage": 200}}),
        ).finalize_completion_input({"conclusion": {"status": "confirmed"}})

        assert isinstance(finalized, CompletionValidationError)
        assert "adjust" in finalized.message

    def test_preview_from_another_parameter_set_is_invalidated_without_mixing(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        old_parameters = {**PARAMETERS, "DBInstanceStorage": 100}
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(preview_parameters=old_parameters),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )
        cost = result.conclusion["selected_candidate_result"]["cost"]

        assert result.conclusion["effective_deployment_parameters"] == PARAMETERS
        assert cost["quote_status"] == "succeeded"
        assert cost["preview_validation"]["succeeded"] is False
        assert result.conclusion["preview_ready_for_create"] is False

    def test_later_template_write_invalidates_anchor(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        records = _records()
        records.append(_record(6, "edit_file", {"path": TEMPLATE_PATH}, {"file_path": TEMPLATE_PATH}, region=None))

        _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=records,
                cwd=tmp_path,
            ),
            _waiting_delta(),
            "validate the authoritative candidate output_path after its latest write",
        )

    def test_missing_anchor_and_override_mismatch_are_local_errors(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        without_anchor = [record for record in _records() if record["tool_name"] != "ros_estimate_template_cost"]
        missing_anchor = _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=without_anchor,
                cwd=tmp_path,
            ),
            _waiting_delta(),
            "ParameterSetAnchor",
        )
        assert "quote_status=not_run" in missing_anchor.message
        _assert_error(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(overrides={"ZoneId": "cn-hangzhou-k"}),
            "does not match ParameterSetAnchor",
        )

    @pytest.mark.parametrize(
        ("quote_result", "status", "monthly"),
        [
            ({"Resources": [], "Currency": "CNY"}, "succeeded", "¥0/month"),
            ({"Resources": {}, "Currency": "CNY"}, "succeeded", "¥0/month"),
            ({"Currency": "CNY"}, "unavailable", "Pricing unavailable"),
            ({"Resources": "invalid", "Currency": "CNY"}, "unavailable", "Pricing unavailable"),
        ],
    )
    def test_free_quote_is_distinct_from_missing_or_invalid_resources(
        self,
        loaded,
        tmp_path,
        quote_result,
        status,
        monthly,
    ):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(quote_result=quote_result),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )
        cost = result.conclusion["selected_candidate_result"]["cost"]

        assert cost["quote_status"] == status
        assert cost["monthly_estimate"] == monthly
        assert cost["resources"] == []

    def test_resource_without_amount_does_not_render_as_free(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        quote_result = {
            "OriginalAmount": "100",
            "TradeAmount": "80",
            "Currency": "CNY",
            "Resources": [{"ResourceType": "ALIYUN::ECS::Instance"}],
        }
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(quote_result=quote_result),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )

        cost = result.conclusion["selected_candidate_result"]["cost"]
        assert cost["quote_status"] == "succeeded"
        assert cost["resources"] == [{"type": "Instance", "cost": "Price unavailable"}]

    def test_ros_resource_mapping_is_normalized_to_monthly_total_and_details(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        quote_result = {
            "Resources": {
                "Ecs": {
                    "Type": "ALIYUN::ECS::Instance",
                    "Success": True,
                    "Properties": {"InstanceType": "ecs.u1-c1m2.large"},
                    "Result": {
                        "Order": {"OriginalAmount": "0.28", "TradeAmount": "0.03", "Currency": "CNY"},
                        "OrderSupplement": {"PriceUnit": "/Hour", "Quantity": 1},
                    },
                },
                "Eip": {
                    "Type": "ALIYUN::VPC::EIP",
                    "Success": True,
                    "Properties": {"Bandwidth": 5},
                    "Result": {
                        "Order": {"OriginalAmount": "5.28", "TradeAmount": "2.24", "Currency": "CNY"},
                        "OrderSupplement": {"PriceUnit": "/Day", "Quantity": 1},
                    },
                },
            }
        }
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(quote_result=quote_result),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )

        cost = result.conclusion["selected_candidate_result"]["cost"]
        assert cost["quote_status"] == "succeeded"
        assert cost["monthly_estimate"] == (
            "¥360.00/month (list price; about ¥88.80/month after contract discount)"
        )
        assert cost["resources"] == [
            {
                "type": "Instance",
                "spec": "InstanceType=ecs.u1-c1m2.large, × 1",
                "cost": "¥201.60/month (list price; about ¥21.60/month after contract discount)",
            },
            {
                "type": "EIP",
                "spec": "Bandwidth=5, × 1",
                "cost": "¥158.40/month (list price; about ¥67.20/month after contract discount)",
            },
        ]

    def test_ros_subscription_period_total_is_normalized_to_monthly_price(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        quote_result = {
            "Resources": {
                "TestRds": {
                    "Type": "ALIYUN::RDS::DBInstance",
                    "Success": True,
                    "Properties": {"DBInstanceClass": "mysql.x2.medium.2c", "DBInstanceStorage": 50},
                    "Result": {
                        "Order": {"OriginalAmount": "630", "TradeAmount": "208.84", "Currency": "CNY"},
                        "OrderSupplement": {
                            "PriceType": "Total",
                            "PeriodUnit": "Month",
                            "Period": 1,
                            "Quantity": 1,
                        },
                    },
                }
            }
        }
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(quote_result=quote_result),
                cwd=tmp_path,
            ),
            _waiting_delta(),
        )

        cost = result.conclusion["selected_candidate_result"]["cost"]
        assert cost["quote_status"] == "succeeded"
        assert cost["monthly_estimate"] == (
            "¥630.00/month (list price; about ¥208.84/month after contract discount)"
        )
        assert cost["resources"] == [
            {
                "type": "DBInstance",
                "spec": "DBInstanceClass=mysql.x2.medium.2c, DBInstanceStorage=50, × 1",
                "cost": "¥630.00/month (list price; about ¥208.84/month after contract discount)",
            }
        ]

    def test_locator_evidence_is_resolved_and_llm_or_code_acceptance_is_preserved(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection(constraint=TOOL_CONSTRAINT)
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=_check(evidence_type="tool")),
        )
        evidence = result.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]["evidence"][0]

        assert evidence == {
            "type": "tool",
            "record_id": "tool-evidence",
            "tool_name": "aliyun_api",
            "result_path": "Items.0.Storage",
            "summary": "tool-evidence field Items.0.Storage",
            "actual_value": 120,
            "product": "rds",
            "action": "DescribeDBInstanceAttribute",
        }

        bad_check = _check(evidence_type="tool")
        bad_check["actual_value"] = 999
        llm_accepted = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=bad_check),
        )
        projected = llm_accepted.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]
        assert projected["status"] == "satisfied"
        assert projected["actual_value"] == 999
        assert projected["evidence"][0]["actual_value"] == 120

    def test_noecho_values_are_redacted_from_constraint_projection_but_kept_for_deployment(self, loaded, tmp_path):
        secret = "Fake-test-password-9!"
        path = tmp_path / TEMPLATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  DBInstanceStorage:
    Type: Number
    Default: 120
  ZoneId:
    Type: String
  MasterUserPassword:
    Type: String
    NoEcho: true
Resources: {}
""",
            encoding="utf-8",
        )
        parameters = {**PARAMETERS, "MasterUserPassword": secret}
        check = _check()
        check["actual_value"] = secret
        check["parameter_values"]["MasterUserPassword"] = secret
        check["evidence"] = [{"type": "template", "parameter_name": "MasterUserPassword"}]
        step = _step(loaded, "materialize_selected_candidate")

        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(parameters=parameters),
                cwd=tmp_path,
            ),
            _waiting_delta(check=check),
        )
        selected = result.conclusion["selected_candidate_result"]
        projected = selected["cost"]["hard_constraint_checks"][0]

        assert selected["cost"]["deployment_parameters"]["MasterUserPassword"] == secret
        assert projected["actual_value"] == "<redacted>"
        assert projected["parameter_values"]["MasterUserPassword"] == "<redacted>"
        assert projected["evidence"][0]["actual_value"] == "<redacted>"

    def test_tool_mode_without_resolvable_evidence_uses_llm_fallback(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection(constraint=TOOL_CONSTRAINT)
        check = _check(evidence_type="tool")
        check["evidence"] = []

        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=check),
        )
        projected = result.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]
        assert projected["status"] == "satisfied"
        assert projected["evidence"] == []

    def test_template_evidence_uses_ros_aware_yaml_and_internal_dotted_path(self, loaded, tmp_path):
        _write_template_with_intrinsic(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        check = _check()
        check["evidence"] = [
            {"type": "template", "template_path": "Parameters.DBInstanceStorage.Default"}
        ]

        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": _selection()},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=check),
        )
        evidence = result.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0][
            "evidence"
        ][0]

        assert evidence["actual_value"] == 120
        assert evidence["template_path"] == "Parameters.DBInstanceStorage.Default"

    def test_context_evidence_is_allowlisted_and_resolved_by_python(self, loaded, tmp_path):
        _write_template(tmp_path)
        step = _step(loaded, "materialize_selected_candidate")
        selection = _selection()
        selection["intent"]["storage"] = 120
        result = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=_check(evidence_type="context")),
        )
        evidence = result.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0][
            "evidence"
        ][0]

        assert evidence == {
            "type": "context",
            "context_path": "solution_selection.intent.storage",
            "summary": "Authoritative context field solution_selection.intent.storage",
            "actual_value": 120,
        }

        outside_allowlist = _check(evidence_type="context")
        outside_allowlist["evidence"][0]["context_path"] = "untrusted.value"
        llm_accepted = _finalize(
            _tool(
                step,
                context_snapshot={"solution_selection": selection, "untrusted": {"value": 120}},
                records=_records(),
                cwd=tmp_path,
            ),
            _waiting_delta(check=outside_allowlist),
        )
        projected = llm_accepted.conclusion["selected_candidate_result"]["cost"]["hard_constraint_checks"][0]
        assert projected["status"] == "satisfied"
        assert projected["evidence"] == []

    def test_reselect_delta_injects_the_outer_rollback_request(self, loaded):
        step = _step(loaded, "materialize_selected_candidate")
        result = _finalize(
            _tool(step, user_message="改成 Serverless"),
            {
                "conclusion": {
                    "status": "reselect_requested",
                    "reselect_reason": "改成 Serverless",
                }
            },
        )

        assert result.conclusion == {
            "status": "reselect_requested",
            "continue_pipeline": True,
            "deployment_confirmed": False,
            "reselect_reason": "改成 Serverless",
        }
        assert result.rollback_request == ("solution_planning_and_selection", "改成 Serverless")


class TestStepThreeProjection:
    def test_status_only_success_injects_real_stack_facts_without_fake_resources(self, loaded):
        step = _step(loaded, "deploying")
        records = [
            _record(
                1,
                "ros_deploy",
                {"action": "create"},
                {
                    "stack_id": "stack-real",
                    "status": "CREATE_COMPLETE",
                    "is_success": True,
                    "outputs": {"Endpoint": "example.internal"},
                },
            )
        ]
        conclusion = _finalize(_tool(step, records=records), {"conclusion": {"status": "success"}}).conclusion

        assert conclusion == {
            "status": "success",
            "stack_id": "stack-real",
            "outputs": {"Endpoint": "example.internal"},
        }
        assert "resources_created" not in conclusion

    def test_failed_status_uses_the_latest_real_failure(self, loaded):
        step = _step(loaded, "deploying")
        records = [
            _record(
                1,
                "ros_deploy",
                {"action": "create"},
                {"stack_id": "stack-failed", "status": "CREATE_FAILED", "is_success": False},
                is_error=True,
                error_summary="quota exceeded",
            )
        ]
        conclusion = _finalize(_tool(step, records=records), {"conclusion": {"status": "failed"}}).conclusion

        assert conclusion == {"status": "failed", "error": "quota exceeded"}

    def test_model_cannot_submit_stack_facts(self, loaded):
        step = _step(loaded, "deploying")
        error = _assert_error(
            _tool(step),
            {"conclusion": {"status": "success", "stack_id": "forged"}},
            "completion_input_schema_validation_failed",
        )

        assert error.phase == "input"


def test_old_selling_steps_do_not_enable_completion_finalization():
    selling = load_pipeline_dir(PIPELINE_DIR.parent / "selling")

    assert all(step.completion_input_schema is None for step in selling.steps)
    assert all(step.completion_enricher is None for step in selling.steps)
    assert all(step.config.get("completion_record_contract") != "v2" for step in selling.steps)
    assert all(step.config.get("hard_constraint_evidence_contract") != "v2" for step in selling.steps)
    assert all(step.config.get("completion_validation_error_limit", 1) == 1 for step in selling.steps)
    assert selling.feature_flags.get("a2a_cleanup_before_pipeline_resume") is not True


def test_solution_first_opts_into_a2a_cleanup_before_pipeline_resume(loaded):
    assert loaded.feature_flags["a2a_cleanup_before_pipeline_resume"] is True


def test_solution_first_alone_opts_into_repl_running_auto_resume(loaded):
    selling = load_pipeline_dir(PIPELINE_DIR.parent / "selling")

    assert loaded.feature_flags["repl_auto_resume_running_on_startup"] is True
    assert selling.feature_flags.get("repl_auto_resume_running_on_startup") is not True


def test_solution_first_limits_each_schema_failure_to_five_diagnostics(loaded):
    assert all(step.config.get("completion_validation_error_limit") == 5 for step in loaded.steps)


def test_old_a2a_artifact_spec_positional_contract_is_unchanged():
    spec = A2AArtifactSpec("conclusion.path", "conclusion.body", "text/yaml", "intermediate", "old.path")

    assert spec.content_from_file is None
    assert spec.media_type == "text/yaml"
    assert spec.role == "intermediate"
    assert spec.supersedes_path == "old.path"
