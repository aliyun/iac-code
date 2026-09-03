"""Authoritative Step 2 completion projection for ``selling_solution_first``."""

from __future__ import annotations

import copy
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from iac_code.i18n import _
from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.ui_contract import parse_deployment_confirmation
from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

__all__ = [
    "enrich_completion_input",
    "on_enter",
    "on_exit",
    "resolve_authoritative_candidate",
    "validate_structured_confirmation",
]


_FILE_MUTATION_TOOLS = frozenset({"write_file", "edit_file"})


def resolve_authoritative_candidate(solution_selection: Any) -> tuple[int | None, dict[str, Any] | None, str]:
    """Resolve the selected candidate only from Step 1's saved candidate array."""

    if not isinstance(solution_selection, dict):
        return None, None, _("solution_selection is missing")
    if solution_selection.get("status") != "selected":
        return None, None, _("solution_selection.status must be 'selected'")
    if solution_selection.get("continue_pipeline") is not True:
        return None, None, _("solution_selection.continue_pipeline is not true")
    raw_candidates = solution_selection.get("candidates")
    if (
        not isinstance(raw_candidates, list)
        or not raw_candidates
        or not all(isinstance(item, dict) for item in raw_candidates)
    ):
        return None, None, _("solution_selection.candidates is empty or invalid")
    candidates: list[dict[str, Any]] = [item for item in raw_candidates if isinstance(item, dict)]
    index = solution_selection.get("selected_candidate_index")
    if isinstance(index, int) and not isinstance(index, bool):
        if not 0 <= index < len(candidates):
            return None, None, _("selected_candidate_index is out of range")
        candidate = candidates[index]
        name = solution_selection.get("selected_candidate_name")
        if isinstance(name, str) and name and candidate.get("name") != name:
            return None, None, _("selected candidate name mismatch")
        return index, candidate, ""
    name = solution_selection.get("selected_candidate_name")
    if not isinstance(name, str) or not name:
        return None, None, _("neither selected_candidate_index nor selected_candidate_name is present")
    matches = [position for position, candidate in enumerate(candidates) if candidate.get("name") == name]
    if len(matches) != 1:
        return None, None, _("selected candidate cannot be mapped uniquely")
    return matches[0], candidates[matches[0]], ""


def on_enter(ctx: PipelineContext) -> None:
    """Record candidate resolver state in Step 1 context for prompts and the deploy gate."""

    selection = ctx.get_conclusion("solution_selection")
    index, candidate, error = resolve_authoritative_candidate(selection)
    if not isinstance(selection, dict):
        return
    selection["selection_valid"] = not error
    selection["selection_error"] = error
    if candidate is not None and index is not None:
        selection["selected_candidate_index"] = index
        selection["selected_candidate_name"] = candidate.get("name")
        selection["selected_candidate"] = copy.deepcopy(candidate)


def on_exit(ctx: PipelineContext, conclusion: dict[str, Any]) -> None:
    """Completion finalization already produced the authoritative conclusion."""

    del ctx, conclusion


def enrich_completion_input(
    *,
    tool_input: dict[str, Any],
    context_snapshot: dict[str, Any],
    tool_result_records: list[dict[str, Any]],
    user_message: str,
    completion_guard_state: dict[str, Any],
    config: Any,
    **_ignored: Any,
) -> dict[str, Any]:
    """Build the canonical public Step 2 result from model semantics and tool facts."""

    raw = tool_input.get("conclusion")
    if not isinstance(raw, dict):
        raise CompletionEnrichmentError("Step 2 completion conclusion must be an object")
    status = raw.get("status")
    if status not in {"awaiting_confirmation", "confirmed", "cancelled", "reselect_requested"}:
        raise CompletionEnrichmentError("invalid Step 2 completion status")

    structured = parse_deployment_confirmation(user_message)
    if status == "cancelled":
        tool_input.pop("rollback_request", None)
        tool_input["conclusion"] = {
            "status": status,
            "continue_pipeline": False,
            "deployment_confirmed": False,
            "cancellation_reason": user_message,
        }
        return tool_input
    if status == "reselect_requested":
        reason = raw.get("reselect_reason")
        if not isinstance(reason, str) or not reason.strip():
            reason = user_message
        if not isinstance(reason, str) or not reason.strip():
            raise CompletionEnrichmentError("reselect_requested requires a non-empty reselect_reason")
        tool_input["conclusion"] = {
            "status": status,
            "continue_pipeline": True,
            "deployment_confirmed": False,
            "reselect_reason": reason.strip(),
        }
        tool_input["rollback_request"] = {
            "target_step": "solution_planning_and_selection",
            "reason": reason.strip(),
        }
        return tool_input

    selection = context_snapshot.get("solution_selection")
    candidate_index, candidate, selection_error = resolve_authoritative_candidate(selection)
    del candidate_index
    if candidate is None:
        raise CompletionEnrichmentError(
            _("authoritative candidate is unavailable: {error}").format(error=selection_error)
        )
    output_path = candidate.get("output_path")
    if not isinstance(output_path, str) or not output_path:
        raise CompletionEnrichmentError("authoritative candidate output_path is missing")

    cwd = str(completion_guard_state.get("cwd") or os.getcwd())
    canonical_output = _canonical_workspace_path(output_path, cwd)
    records = _ordered_records(tool_result_records)
    last_mutation = max(
        (
            sequence
            for sequence, record in records
            if record.get("tool_name") in _FILE_MUTATION_TOOLS
            and _record_template_path(record, cwd) == canonical_output
        ),
        default=0,
    )
    validate_record = _latest_record(
        records,
        tool_name="ros_validate_template",
        after=last_mutation,
        predicate=lambda record: _record_template_path(record, cwd) == canonical_output,
    )
    if validate_record is None or validate_record.get("is_error"):
        raise CompletionEnrichmentError("validate the authoritative candidate output_path after its latest write")

    anchor = _latest_record(
        records,
        tool_name="ros_estimate_template_cost",
        after=last_mutation,
        predicate=lambda record: _record_template_path(record, cwd) == canonical_output,
    )
    if anchor is None:
        raise CompletionEnrichmentError(
            "ParameterSetAnchor is missing (quote_status=not_run): run ros_estimate_template_cost for output_path"
        )
    anchor_input = _dict_value(anchor.get("input"))
    parameters = anchor_input.get("parameters")
    if not isinstance(parameters, dict):
        raise CompletionEnrichmentError("ParameterSetAnchor input.parameters must be an object")
    region = _record_region(anchor)
    if not region:
        raise CompletionEnrichmentError("ParameterSetAnchor effective region is unavailable; pass region_id explicitly")

    saved_plan = context_snapshot.get("selected_plan")
    saved_plan = saved_plan if isinstance(saved_plan, dict) else {}
    saved_overrides = saved_plan.get("parameter_overrides")
    saved_overrides = saved_overrides if isinstance(saved_overrides, dict) else {}
    overrides = raw.get("parameter_overrides", saved_overrides)
    if not isinstance(overrides, dict):
        raise CompletionEnrichmentError("parameter_overrides must be an object")

    one_shot_overrides = (
        status == "confirmed"
        and structured is not None
        and structured.action == "confirm"
        and structured.parameter_overrides_provided
        and getattr(config, "confirmation_accepts_parameter_overrides", False) is True
    )
    if one_shot_overrides and structured is not None:
        # Opt-in capability: an explicit structured confirm is a final authorization even when it carries
        # parameters that differ from the last quote input. Python merges and validates them here instead of
        # forcing another materialize/Preview/pricing round and a second user confirmation.
        submitted = copy.deepcopy(structured.parameter_overrides)
        _validate_parameter_overrides(_load_template(canonical_output), submitted)
        overrides = {**saved_overrides, **submitted}
        effective_parameters = {**parameters, **submitted}
    else:
        submitted = {}
        for name, value in overrides.items():
            if name not in parameters or parameters[name] != value:
                raise CompletionEnrichmentError(
                    f"parameter_overrides.{name} does not match ParameterSetAnchor; re-run Preview and pricing"
                )
        effective_parameters = dict(parameters)
    parameters_changed = effective_parameters != parameters

    preview = _latest_record(
        records,
        tool_name="ros_preview_template",
        after=last_mutation,
        predicate=lambda record: _record_matches_anchor(
            record, canonical_output, effective_parameters, region, cwd
        ),
    )
    preview_validation = _preview_projection(preview, output_path, effective_parameters, region)
    quote = _quote_projection(anchor)

    solution_summary = raw.get("solution_summary")
    if not isinstance(solution_summary, str) or not solution_summary.strip():
        saved_result = saved_plan.get("selected_candidate_result")
        solution_summary = saved_result.get("solution_summary") if isinstance(saved_result, dict) else None
    if not isinstance(solution_summary, str) or not solution_summary.strip():
        raise CompletionEnrichmentError("awaiting_confirmation requires a new non-empty solution_summary")

    missing = raw.get("missing_deployment_parameters")
    if missing is None and status == "confirmed":
        missing = _saved_cost_value(saved_plan, "missing_deployment_parameters", [])
    if not isinstance(missing, list) or not all(isinstance(item, dict) for item in missing):
        raise CompletionEnrichmentError("missing_deployment_parameters must be an array of objects")
    if one_shot_overrides and submitted:
        # A one-shot confirm may itself supply the values the previous quote reported as missing. Those gaps
        # are closed by `submitted` (already name/type/constraint validated above), so they must not be
        # inherited verbatim from the saved quote and trip the user-required guard below, which would send
        # this deterministic confirmation back through an agent recovery round.
        missing = [item for item in missing if item.get("name") not in submitted]
    user_required = [copy.deepcopy(item) for item in missing if item.get("classification") == "user_required"]

    input_checks = raw.get("hard_constraint_checks")
    if input_checks is None and status == "confirmed":
        saved_checks = _saved_cost_value(saved_plan, "hard_constraint_checks", [])
        checks = copy.deepcopy(saved_checks) if isinstance(saved_checks, list) else []
    else:
        checks = _project_hard_constraint_checks(
            input_checks,
            selection=selection,
            context_snapshot=context_snapshot,
            records=[record for _, record in records],
            parameters=effective_parameters,
            template_path=canonical_output,
            allowed_context_paths=tuple(getattr(config, "completion_context_paths", ())),
        )

    options = _confirmation_options(selection)
    result = {
        "solution_summary": solution_summary.strip(),
        "template": {"file_path": output_path, "region": region},
        "cost": {
            **quote,
            "deployment_parameters": copy.deepcopy(parameters),
            "missing_deployment_parameters": copy.deepcopy(missing),
            "user_required_missing_parameters": user_required,
            "hard_constraint_checks": checks,
            "preview_validation": preview_validation,
        },
    }
    conclusion: dict[str, Any] = {
        "status": status,
        "continue_pipeline": True,
        "deployment_confirmed": status == "confirmed",
        "selection_valid": True,
        "selected_candidate_result": result,
        "template_url": output_path,
        "parameter_overrides": copy.deepcopy(overrides),
        "effective_deployment_parameters": copy.deepcopy(effective_parameters),
        "preview_ready_for_create": (
            preview_validation.get("succeeded") is True and not missing and not parameters_changed
        ),
    }
    if status == "awaiting_confirmation":
        conclusion["user_prompt"] = _("Choose the next action")
        conclusion["options"] = options
    else:
        if user_required:
            raise CompletionEnrichmentError("confirmed completion cannot contain user-required parameter gaps")
        conclusion["confirmation"] = {
            "action": "confirm",
            "input_type": "structured" if structured is not None else "natural_language",
            "user_input": user_message,
            # The confirmation mirrors exactly what this request carried, while the top-level override map
            # accumulates it onto the parameters the previous quote already used.
            "parameter_overrides": copy.deepcopy(submitted) if one_shot_overrides else copy.deepcopy(overrides),
        }
    tool_input.pop("rollback_request", None)
    tool_input["conclusion"] = conclusion
    return tool_input


def validate_structured_confirmation(
    *,
    conclusion: dict[str, Any],
    user_message: str,
    cwd: str = "",
    **_ignored: Any,
) -> str | None:
    """Pre-check explicitly submitted deployment parameters while the step still waits for input.

    Returns a specific error message when the structured confirmation carries illegal parameters, so the
    step can keep waiting for corrected input instead of failing the deterministic confirmation later.
    """

    structured = parse_deployment_confirmation(user_message)
    if structured is None or structured.action != "confirm" or not structured.parameter_overrides_provided:
        return None
    if not isinstance(conclusion, dict) or conclusion.get("status") != "awaiting_confirmation":
        return None
    template_url = conclusion.get("template_url")
    if not isinstance(template_url, str) or not template_url:
        return None
    try:
        template = _load_template(_canonical_workspace_path(template_url, str(cwd or os.getcwd())))
    except CompletionEnrichmentError:
        # The certified template is unavailable here; the completion projection raises the authoritative error.
        return None
    try:
        _validate_parameter_overrides(template, structured.parameter_overrides)
    except CompletionEnrichmentError as error:
        return str(error)
    return None


def _validate_parameter_overrides(template: dict[str, Any], submitted: dict[str, Any]) -> None:
    """Validate confirmed parameter overrides against the certified template declarations.

    Only the submitted parameters are validated: pre-existing gaps in the solved parameter set are already
    reflected by ``preview_ready_for_create`` and re-validated by the normal deployment path in Step 3.
    Error messages name the parameter and the violated constraint and never echo the submitted value.
    """

    declarations = template.get("Parameters")
    declarations = declarations if isinstance(declarations, dict) else {}
    for name, value in submitted.items():
        if not isinstance(name, str) or not name:
            raise CompletionEnrichmentError("Parameter names must be non-empty strings")
        declaration = declarations.get(name)
        if not isinstance(declaration, dict):
            raise CompletionEnrichmentError(
                _("Parameter {name} is not declared in template Parameters").format(name=name)
            )
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            if "Default" not in declaration:
                raise CompletionEnrichmentError(
                    _constraint_error(
                        _("Parameter {name} is required and cannot be empty").format(name=name),
                        declaration,
                    )
                )
            continue
        error = _parameter_value_error(name, declaration, value)
        if error:
            raise CompletionEnrichmentError(error)


def _parameter_value_error(name: str, declaration: dict[str, Any], value: Any) -> str:
    declared_type = declaration.get("Type")
    declared_type = declared_type if isinstance(declared_type, str) and declared_type else "String"
    if not _parameter_type_matches(declared_type, value):
        return _constraint_error(
            _("Parameter {name} must match the declared template type {declared_type}").format(
                name=name,
                declared_type=declared_type,
            ),
            declaration,
        )

    allowed = declaration.get("AllowedValues")
    if isinstance(allowed, list) and allowed and not any(_scalar_equal(value, item) for item in allowed):
        return _constraint_error(
            _("Parameter {name} is outside the template AllowedValues").format(name=name),
            declaration,
        )

    pattern = declaration.get("AllowedPattern")
    if isinstance(pattern, str) and pattern and isinstance(value, str):
        try:
            matched = re.fullmatch(pattern, value) is not None
        except re.error:
            matched = True
        if not matched:
            return _constraint_error(
                _("Parameter {name} does not match the template AllowedPattern").format(name=name),
                declaration,
            )

    numeric = _decimal(value) if not isinstance(value, (dict, list)) else None
    if numeric is not None:
        minimum = _decimal(declaration.get("MinValue"))
        maximum = _decimal(declaration.get("MaxValue"))
        if minimum is not None and numeric < minimum:
            return _constraint_error(
                _("Parameter {name} is below the template MinValue {minimum}").format(
                    name=name,
                    minimum=minimum,
                ),
                declaration,
            )
        if maximum is not None and numeric > maximum:
            return _constraint_error(
                _("Parameter {name} exceeds the template MaxValue {maximum}").format(
                    name=name,
                    maximum=maximum,
                ),
                declaration,
            )

    if isinstance(value, str):
        min_length = _decimal(declaration.get("MinLength"))
        max_length = _decimal(declaration.get("MaxLength"))
        length = Decimal(len(value))
        if min_length is not None and length < min_length:
            return _constraint_error(
                _("Parameter {name} is shorter than the template MinLength {min_length}").format(
                    name=name,
                    min_length=min_length,
                ),
                declaration,
            )
        if max_length is not None and length > max_length:
            return _constraint_error(
                _("Parameter {name} is longer than the template MaxLength {max_length}").format(
                    name=name,
                    max_length=max_length,
                ),
                declaration,
            )
    return ""


def _parameter_type_matches(declared_type: str, value: Any) -> bool:
    if declared_type == "Number":
        return _decimal(value) is not None
    if declared_type == "Boolean":
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"true", "false"}
        return value in (0, 1)
    if declared_type == "Json":
        return isinstance(value, (dict, list))
    if declared_type == "CommaDelimitedList":
        if isinstance(value, str):
            return True
        return isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value)
    # String and every ROS-specific string alias (ALIYUN::ECS::Instance::ZoneId, ...) accept plain scalars.
    return isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _scalar_equal(value: Any, allowed: Any) -> bool:
    if value == allowed:
        return True
    if isinstance(value, (dict, list)) or isinstance(allowed, (dict, list)):
        return False
    left = _decimal(value)
    right = _decimal(allowed)
    if left is not None and right is not None:
        return left == right
    return str(value) == str(allowed)


def _constraint_error(message: str, declaration: dict[str, Any]) -> str:
    description = declaration.get("ConstraintDescription")
    if isinstance(description, str) and description.strip():
        return _("{message}: {description}").format(message=message, description=description.strip())
    return message


def _ordered_records(records: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    ordered: list[tuple[int, dict[str, Any]]] = []
    for index, record in enumerate(records, start=1):
        sequence = record.get("sequence")
        ordered.append((sequence if isinstance(sequence, int) else index, record))
    return ordered


def _latest_record(
    records: list[tuple[int, dict[str, Any]]],
    *,
    tool_name: str,
    after: int,
    predicate: Any,
) -> dict[str, Any] | None:
    for sequence, record in reversed(records):
        if sequence > after and record.get("tool_name") == tool_name and predicate(record):
            return record
    return None


def _canonical_workspace_path(value: str, cwd: str) -> str:
    root = Path(cwd).expanduser().resolve(strict=False)
    path = Path(os.path.expandvars(value)).expanduser()
    path = (root / path).resolve(strict=False) if not path.is_absolute() else path.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise CompletionEnrichmentError("authoritative template path is outside the workspace") from error
    return os.path.normcase(str(path))


def _record_template_path(record: dict[str, Any], cwd: str) -> str:
    result = _dict_value(record.get("result"))
    canonical = result.get("canonical_file_path")
    if isinstance(canonical, str) and canonical:
        return os.path.normcase(canonical)
    tool_input = _dict_value(record.get("input"))
    path = tool_input.get("template_url") or tool_input.get("path") or tool_input.get("file_path")
    if not isinstance(path, str) or not path or "://" in path:
        return ""
    try:
        return _canonical_workspace_path(path, cwd)
    except CompletionEnrichmentError:
        return ""


def _record_region(record: dict[str, Any]) -> str:
    value = record.get("effective_region_id")
    if isinstance(value, str) and value:
        return value
    tool_input = _dict_value(record.get("input"))
    value = tool_input.get("region_id")
    return value if isinstance(value, str) else ""


def _record_matches_anchor(
    record: dict[str, Any],
    path: str,
    parameters: dict[str, Any],
    region: str,
    cwd: str,
) -> bool:
    tool_input = _dict_value(record.get("input"))
    return (
        _record_template_path(record, cwd) == path
        and tool_input.get("parameters") == parameters
        and _record_region(record) == region
    )


def _preview_projection(
    record: dict[str, Any] | None,
    template_url: str,
    parameters: dict[str, Any],
    region: str,
) -> dict[str, Any]:
    if record is None:
        return {
            "succeeded": False,
            "error": _("No Preview matches the final template, parameters, and region"),
        }
    tool_input = _dict_value(record.get("input"))
    projection: dict[str, Any] = {
        "succeeded": not bool(record.get("is_error")),
        "template_url": template_url,
        "parameters": copy.deepcopy(parameters),
        "region_id": region,
    }
    stack_name = tool_input.get("stack_name")
    if isinstance(stack_name, str) and stack_name:
        projection["stack_name"] = stack_name
    if record.get("is_error"):
        projection["error"] = str(record.get("error_summary") or _("Preview failed"))
    return projection


def _quote_projection(anchor: dict[str, Any]) -> dict[str, Any]:
    if anchor.get("is_error"):
        return {
            "quote_status": "failed",
            "monthly_estimate": _("Pricing failed"),
            "currency": "CNY",
            "resources": [],
            "error": str(anchor.get("error_summary") or _("ROS estimate failed")),
        }
    result = anchor.get("result")
    if not isinstance(result, dict):
        return _unavailable_quote("ROS estimate result is unavailable")
    resources = _normalize_quote_resources(result.get("Resources"))
    if resources is None:
        return _unavailable_quote("ROS estimate response has no valid Resources array")
    original = _decimal(result.get("OriginalAmount"))
    trade = _decimal(result.get("TradeAmount"))
    if original is None:
        original = _sum_resource_amount(resources, "OriginalAmount")
    if trade is None:
        trade = _sum_resource_amount(resources, "TradeAmount")
    if original is None and trade is None:
        if not resources:
            original = Decimal(0)
            trade = Decimal(0)
        else:
            return _unavailable_quote("ROS estimate response contains resources but no usable amount")
    resource_currencies = {
        str(item.get("Currency")).upper()
        for item in resources
        if isinstance(item.get("Currency"), str) and item.get("Currency")
    }
    currency = result.get("Currency") or (next(iter(resource_currencies)) if len(resource_currencies) == 1 else "CNY")
    if str(currency).upper() != "CNY":
        return _unavailable_quote(
            _("Unsupported ROS estimate currency: {currency}").format(currency=currency)
        )
    if any(item != "CNY" for item in resource_currencies):
        return _unavailable_quote(
            _("Unsupported ROS estimate resource currencies: {currencies}").format(
                currencies=sorted(resource_currencies)
            )
        )
    warning = ""
    if original is None or trade is None:
        warning = _("ROS estimate response contains only one price basis")
    projection: dict[str, Any] = {
        "quote_status": "succeeded",
        "monthly_estimate": _format_monthly_amount(original, trade),
        "currency": "CNY",
        "resources": [_resource_cost(item) for item in resources if isinstance(item, dict)],
        "api_raw_summary": warning or _("ROS estimate response normalized from OriginalAmount/TradeAmount"),
    }
    return projection


def _normalize_quote_resources(raw_resources: Any) -> list[dict[str, Any]] | None:
    if isinstance(raw_resources, list):
        return raw_resources if all(isinstance(item, dict) for item in raw_resources) else None
    if not isinstance(raw_resources, dict):
        return None
    normalized: list[dict[str, Any]] = []
    for resource_name, raw_item in raw_resources.items():
        if not isinstance(raw_item, dict) or raw_item.get("Success") is False:
            return None
        result = raw_item.get("Result")
        if not isinstance(result, dict):
            return None
        order = result.get("Order")
        supplement = result.get("OrderSupplement")
        if not isinstance(order, dict) or not isinstance(supplement, dict):
            return None
        factor = _monthly_price_factor(supplement)
        if factor is None:
            return None
        item: dict[str, Any] = {
            "ResourceName": str(resource_name),
            "ResourceType": raw_item.get("Type") or _("Cloud resource"),
        }
        for field in ("OriginalAmount", "TradeAmount"):
            amount = _decimal(order.get(field))
            if amount is not None:
                item[field] = amount * factor
        currency = order.get("Currency")
        if isinstance(currency, str) and currency:
            item["Currency"] = currency
        spec = _quote_resource_spec(raw_item.get("Properties"), supplement)
        if spec:
            item["Spec"] = spec
        normalized.append(item)
    return normalized


def _monthly_price_factor(supplement: dict[str, Any]) -> Decimal | None:
    normalized = str(supplement.get("PriceUnit") or "").strip().lower().replace(" ", "")
    factors = {
        "/hour": Decimal(24 * 30),
        "hour": Decimal(24 * 30),
        "/day": Decimal(30),
        "day": Decimal(30),
        "/week": Decimal(30) / Decimal(7),
        "week": Decimal(30) / Decimal(7),
        "/month": Decimal(1),
        "month": Decimal(1),
        "/year": Decimal(1) / Decimal(12),
        "year": Decimal(1) / Decimal(12),
    }
    factor = factors.get(normalized)
    if factor is not None:
        return factor

    # GetTemplateEstimateCost commonly returns a total price for a subscription period using
    # PeriodUnit/Period instead of PriceUnit (for example, one prepaid month). Normalize that
    # total to a monthly amount without changing the legacy PriceUnit interpretation above.
    period_unit = str(supplement.get("PeriodUnit") or "").strip().lower().replace(" ", "")
    period = _decimal(supplement.get("Period")) or Decimal(1)
    if period <= 0:
        return None
    total_price_factors = {
        "hour": Decimal(24 * 30),
        "day": Decimal(30),
        "week": Decimal(30) / Decimal(7),
        "month": Decimal(1),
        "year": Decimal(1) / Decimal(12),
    }
    period_factor = total_price_factors.get(period_unit)
    return period_factor / period if period_factor is not None else None


def _quote_resource_spec(properties: Any, supplement: dict[str, Any]) -> str:
    if not isinstance(properties, dict):
        properties = {}
    parts: list[str] = []
    for key in (
        "InstanceType",
        "DBInstanceClass",
        "DBInstanceStorage",
        "SystemDiskSize",
        "Bandwidth",
        "InternetChargeType",
    ):
        value = properties.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            parts.append(f"{key}={value}")
    quantity = supplement.get("Quantity")
    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        parts.append(f"× {quantity:g}")
    return ", ".join(parts)


def _unavailable_quote(message: str) -> dict[str, Any]:
    return {
        "quote_status": "unavailable",
        "monthly_estimate": _("Pricing unavailable"),
        "currency": "CNY",
        "resources": [],
        "error": message,
        "api_raw_summary": message,
    }


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _sum_resource_amount(resources: list[Any], field: str) -> Decimal | None:
    values = [_decimal(item.get(field)) for item in resources if isinstance(item, dict)]
    concrete = [value for value in values if value is not None]
    return sum(concrete, Decimal(0)) if concrete and len(concrete) == len(values) else None


def _format_money(value: Decimal) -> str:
    return f"¥{value:,.2f}"


def _format_monthly_amount(original: Decimal | None, trade: Decimal | None) -> str:
    if original == 0 and trade == 0:
        return _("¥0/month")
    if original is not None and trade is not None:
        return _("{original}/month (list price; about {trade}/month after contract discount)").format(
            original=_format_money(original),
            trade=_format_money(trade),
        )
    value = original if original is not None else trade
    return _("{value}/month").format(value=_format_money(value or Decimal(0)))


def _resource_cost(item: dict[str, Any]) -> dict[str, Any]:
    resource_type = item.get("ResourceType") or item.get("Type") or item.get("ResourceName") or _("Cloud resource")
    readable_type = str(resource_type).split("::")[-1]
    original = _decimal(item.get("OriginalAmount"))
    trade = _decimal(item.get("TradeAmount"))
    cost = _("Price unavailable") if original is None and trade is None else _format_monthly_amount(original, trade)
    result: dict[str, Any] = {"type": readable_type, "cost": cost}
    spec = item.get("Spec") or item.get("ResourceSpec") or item.get("Description")
    if spec:
        result["spec"] = str(spec)
    return result


def _saved_cost_value(plan: dict[str, Any], field: str, default: Any) -> Any:
    selected_result = plan.get("selected_candidate_result")
    cost = selected_result.get("cost") if isinstance(selected_result, dict) else None
    return cost.get(field, default) if isinstance(cost, dict) else default


def _confirmation_options(selection: Any) -> list[dict[str, str]]:
    defaults = {
        "confirm": {
            "action": "confirm",
            "name": _("Confirm deployment"),
            "summary": _("Create cloud resources using the current solution and parameters"),
        },
        "reselect": {
            "action": "reselect",
            "name": _("Choose another solution"),
            "summary": _("Return to solution planning and choose again"),
        },
        "cancel": {
            "action": "cancel",
            "name": _("Cancel"),
            "summary": _("End the workflow without creating cloud resources"),
        },
    }
    raw_candidates = selection.get("candidates") if isinstance(selection, dict) else None
    actions = ["confirm", "cancel"] if not isinstance(raw_candidates, list) or len(raw_candidates) <= 1 else [
        "confirm",
        "reselect",
        "cancel",
    ]
    return [copy.deepcopy(defaults[action]) for action in actions]


def _project_hard_constraint_checks(
    raw_checks: Any,
    *,
    selection: Any,
    context_snapshot: dict[str, Any],
    records: list[dict[str, Any]],
    parameters: dict[str, Any],
    template_path: str,
    allowed_context_paths: tuple[str, ...],
) -> list[dict[str, Any]]:
    constraints = _authoritative_constraints(selection)
    if not constraints:
        return []
    if not isinstance(raw_checks, list):
        raise CompletionEnrichmentError("hard_constraint_checks must cover every authoritative constraint")
    by_id: dict[str, dict[str, Any]] = {}
    for check in raw_checks:
        constraint_id = check.get("constraint_id") if isinstance(check, dict) else None
        if not isinstance(constraint_id, str) or not constraint_id or constraint_id in by_id:
            raise CompletionEnrichmentError("hard_constraint_checks must contain unique constraint_id values")
        by_id[constraint_id] = check
    expected = {str(item.get("id")): item for item in constraints}
    if set(by_id) != set(expected):
        raise CompletionEnrichmentError("hard_constraint_checks must cover each authoritative constraint exactly once")

    template = _load_template(template_path)
    noecho_values = _noecho_parameter_values(template, parameters)
    record_by_id = {
        str(record.get("record_id")): record
        for record in records
        if isinstance(record.get("record_id"), str) and record.get("record_id")
    }
    projected: list[dict[str, Any]] = []
    for constraint in constraints:
        constraint_id = str(constraint["id"])
        check = by_id[constraint_id]
        actual = check.get("actual_value")
        parameter_values = check.get("parameter_values", {})
        if not isinstance(parameter_values, dict):
            raise CompletionEnrichmentError(
                _("hard constraint {constraint_id} parameter_values must be an object").format(
                    constraint_id=constraint_id
                )
            )
        evidence = check.get("evidence", [])
        if not isinstance(evidence, list):
            raise CompletionEnrichmentError(
                _("hard constraint {constraint_id} evidence must be an array").format(constraint_id=constraint_id)
            )
        projected_evidence: list[dict[str, Any]] = []
        for locator in evidence:
            try:
                projected_evidence.append(
                    _redact_sensitive_matches(
                        _project_evidence(
                            locator,
                            context_snapshot=context_snapshot,
                            allowed_context_paths=allowed_context_paths,
                            template=template,
                            parameters=parameters,
                            record_by_id=record_by_id,
                        ),
                        noecho_values,
                    )
                )
            except CompletionEnrichmentError:
                # A missing/unresolvable locator makes Python verification fail, but it must not bypass
                # the legacy LLM-or-code acceptance rule. The canonical conclusion keeps only verified evidence.
                continue
        actual_unit = check.get("actual_unit")
        status = check.get("status")
        if status not in {"satisfied", "conflict", "unresolved"}:
            raise CompletionEnrichmentError(
                _("hard constraint {constraint_id} requires an LLM status").format(constraint_id=constraint_id)
            )
        projected_check: dict[str, Any] = {
            "constraint": _redact_sensitive_matches(copy.deepcopy(constraint), noecho_values),
            "status": status,
            "actual_value": _redact_sensitive_matches(copy.deepcopy(actual), noecho_values),
            "parameter_values": _redact_sensitive_matches(copy.deepcopy(parameter_values), noecho_values),
            "evidence": projected_evidence,
        }
        if isinstance(actual_unit, str):
            projected_check["actual_unit"] = actual_unit
        projected.append(projected_check)
    return projected


def _authoritative_constraints(selection: Any) -> list[dict[str, Any]]:
    intent = selection.get("intent") if isinstance(selection, dict) else None
    constraints = intent.get("hard_constraints") if isinstance(intent, dict) else None
    if constraints is None and isinstance(selection, dict):
        candidate = selection.get("selected_candidate")
        constraints = candidate.get("hard_constraints") if isinstance(candidate, dict) else None
    return [copy.deepcopy(item) for item in constraints] if isinstance(constraints, list) else []


def _noecho_parameter_values(template: dict[str, Any], parameters: dict[str, Any]) -> list[Any]:
    declarations = template.get("Parameters")
    if not isinstance(declarations, dict):
        return []
    values: list[Any] = []
    for name, declaration in declarations.items():
        if not isinstance(name, str) or name not in parameters or not isinstance(declaration, dict):
            continue
        noecho = declaration.get("NoEcho")
        if noecho is True or (isinstance(noecho, str) and noecho.strip().lower() == "true"):
            value = parameters[name]
            if value not in (None, ""):
                values.append(value)
    return values


def _redact_sensitive_matches(value: Any, sensitive_values: list[Any]) -> Any:
    if isinstance(value, dict):
        return {key: _redact_sensitive_matches(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive_matches(item, sensitive_values) for item in value]
    for sensitive in sensitive_values:
        if value == sensitive:
            return "<redacted>"
        if isinstance(value, str) and isinstance(sensitive, str) and sensitive and sensitive in value:
            value = value.replace(sensitive, "<redacted>")
    return value


def _project_evidence(
    locator: Any,
    *,
    context_snapshot: dict[str, Any],
    allowed_context_paths: tuple[str, ...],
    template: dict[str, Any],
    parameters: dict[str, Any],
    record_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(locator, dict):
        raise CompletionEnrichmentError("hard constraint evidence locator must be an object")
    evidence_type = locator.get("type")
    if evidence_type == "tool":
        record_id = locator.get("record_id")
        result_path = locator.get("result_path")
        record = record_by_id.get(str(record_id))
        if record is None or record.get("is_error") or not isinstance(result_path, str) or not result_path:
            raise CompletionEnrichmentError(
                "tool evidence must reference a successful stable record_id and result_path"
            )
        if locator.get("tool_name") and locator.get("tool_name") != record.get("tool_name"):
            raise CompletionEnrichmentError("tool evidence tool_name does not match record_id")
        actual = _resolve_dotted(record.get("result"), result_path)
        if actual is _MISSING:
            raise CompletionEnrichmentError("tool evidence result_path cannot be resolved")
        tool_input = _dict_value(record.get("input"))
        result = {
            "type": "tool",
            "record_id": str(record_id),
            "tool_name": str(record.get("tool_name") or ""),
            "result_path": result_path,
            "summary": _("{record_id} field {result_path}").format(
                record_id=record_id,
                result_path=result_path,
            ),
            "actual_value": copy.deepcopy(actual),
        }
        for key in ("product", "action"):
            if tool_input.get(key):
                result[key] = tool_input[key]
        return result
    if evidence_type == "context":
        path = locator.get("context_path")
        if not isinstance(path, str) or not _allowed_context_path(path, allowed_context_paths):
            raise CompletionEnrichmentError("context evidence path is outside the configured allowlist")
        actual = _resolve_dotted(context_snapshot, path)
        if actual is _MISSING:
            raise CompletionEnrichmentError("context evidence path cannot be resolved")
        return {
            "type": "context",
            "context_path": path,
            "summary": _("Authoritative context field {path}").format(path=path),
            "actual_value": copy.deepcopy(actual),
        }
    if evidence_type == "template":
        template_field = locator.get("template_path")
        parameter_name = locator.get("parameter_name")
        if bool(template_field) == bool(parameter_name):
            raise CompletionEnrichmentError("template evidence requires exactly one of template_path or parameter_name")
        if isinstance(parameter_name, str) and parameter_name:
            if parameter_name not in parameters:
                raise CompletionEnrichmentError("template evidence parameter_name is not in anchor parameters")
            return {
                "type": "template",
                "parameter_name": parameter_name,
                "summary": _("Final parameter {parameter_name}").format(parameter_name=parameter_name),
                "actual_value": copy.deepcopy(parameters[parameter_name]),
            }
        if not isinstance(template_field, str) or not template_field:
            raise CompletionEnrichmentError("template evidence template_path is invalid")
        actual = _resolve_dotted(template, template_field)
        if actual is _MISSING:
            raise CompletionEnrichmentError("template evidence template_path cannot be resolved")
        return {
            "type": "template",
            "template_path": template_field,
            "summary": _("Final template field {template_field}").format(template_field=template_field),
            "actual_value": copy.deepcopy(actual),
        }
    raise CompletionEnrichmentError("hard constraint evidence type must be context, template, or tool")


def _allowed_context_path(path: str, allowed: tuple[str, ...]) -> bool:
    return bool(path) and ".." not in path and "*" not in path and any(
        path == prefix or path.startswith(prefix + ".") for prefix in allowed
    )


def _load_template(path: str) -> dict[str, Any]:
    try:
        parsed = ros_yaml_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise CompletionEnrichmentError("the final validated template cannot be parsed") from error
    if not isinstance(parsed, dict):
        raise CompletionEnrichmentError("the final validated template root must be an object")
    return parsed


_MISSING = object()


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _resolve_dotted(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part or part in {"..", "*"}:
            return _MISSING
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and 0 <= int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current
