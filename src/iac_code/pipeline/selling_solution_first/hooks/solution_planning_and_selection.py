"""Authoritative completion projection for solution planning and selection."""

from __future__ import annotations

import copy
import re
from typing import Any

from iac_code.i18n import _
from iac_code.pipeline.engine.complete_step_tool import CompletionEnrichmentError
from iac_code.pipeline.selling_solution_first.tools.candidate_planning_records import (
    CandidateOutlineBatch,
    detail_record_matches,
    latest_candidate_detail_records,
    latest_candidate_outline_batch,
)

__all__ = ["enrich_completion_input"]

# 说服力字段与各自的最少条目数。缺失或空数组时方案卡只剩一句概述，用户无从判断为什么该选它，
# 因此这里和 completion_input_schema 一样把它们当硬要求，而不是可选补充。
_REQUIRED_DECISION_NOTES: tuple[tuple[str, int], ...] = (
    ("why_recommended", 1),
    ("problems_solved", 1),
    ("pros", 2),
    ("cons", 1),
)

_COMPOSED_MONTHLY_PRICE_RE = re.compile(
    r"^\s*(?:约\s*)?(?:"
    r"[¥￥]\s*[\d,.]+(?:\s*(?:[-~～—–]|至|到)\s*(?:[¥￥]\s*)?[\d,.]+)?"
    r"\s*(?:元|CNY|RMB)?\s*(?:(?:[/／]\s*)?月|每月)?"
    r"|[\d,.]+(?:\s*(?:[-~～—–]|至|到)\s*(?:[¥￥]\s*)?[\d,.]+)?"
    r"\s*(?:元\s*(?:(?:[/／]\s*)?月)?|(?:[/／]\s*)月|每月)"
    r"|(?:CNY|RMB)\s*[\d,.]+(?:\s*(?:[-~～—–]|至|到)\s*(?:CNY|RMB)?\s*[\d,.]+)?"
    r"\s*(?:(?:[/／]\s*)?月|每月)?"
    r"|(?:免费|零费用|无费用)\s*(?:(?:[/／]\s*)?月|每月)?"
    r")\s*$",
    re.IGNORECASE,
)


def enrich_completion_input(
    *,
    tool_input: dict[str, Any],
    context_snapshot: dict[str, Any],
    tool_result_records: list[dict[str, Any]],
    user_message: str,
    **_ignored: Any,
) -> dict[str, Any]:
    """Expand the model's semantic delta into the stable Step 1 runtime conclusion."""

    raw = tool_input.get("conclusion")
    if not isinstance(raw, dict):
        raise CompletionEnrichmentError("Step 1 completion conclusion must be an object")
    status = raw.get("status")
    if status not in {"awaiting_selection", "selected", "rejected"}:
        raise CompletionEnrichmentError("Step 1 status must be awaiting_selection, selected, or rejected")

    if status == "rejected":
        reason = raw.get("rejection_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise CompletionEnrichmentError("rejected completion requires a non-empty rejection_reason")
        tool_input["conclusion"] = {
            "status": "rejected",
            "continue_pipeline": False,
            "is_infra_intent": False,
            "rejection_reason": reason.strip(),
        }
        return tool_input

    saved = context_snapshot.get("solution_selection")
    saved = saved if isinstance(saved, dict) else {}
    intent = raw.get("intent", saved.get("intent"))
    if not isinstance(intent, dict):
        raise CompletionEnrichmentError("awaiting_selection requires a structured intent")
    resource_intents = intent.get("resource_intents")
    if not isinstance(resource_intents, list) or not all(isinstance(item, dict) for item in resource_intents):
        raise CompletionEnrichmentError("intent.resource_intents must be an array of objects")
    hard_constraints = intent.get("hard_constraints", [])
    if not isinstance(hard_constraints, list) or not all(isinstance(item, dict) for item in hard_constraints):
        raise CompletionEnrichmentError("intent.hard_constraints must be an array of objects")

    saved_candidates = saved.get("candidates")
    saved_candidates = saved_candidates if isinstance(saved_candidates, list) else []
    saved_candidate_set_id = saved.get("candidate_set_id")
    batch = latest_candidate_outline_batch(tool_result_records)

    if status == "selected":
        if batch is not None and batch.candidate_set_id != saved_candidate_set_id:
            raise CompletionEnrichmentError(
                "selected completion is blocked because a new candidate batch was generated; "
                "complete the new batch with status awaiting_selection before the user selects a candidate"
            )
        raw_candidates = saved_candidates
        candidate_set_id = saved_candidate_set_id
        if not raw_candidates:
            raise CompletionEnrichmentError("selected completion requires saved authoritative candidates")
    elif batch is not None and batch.candidate_set_id != saved_candidate_set_id:
        raw_candidates = _candidates_from_current_batch(batch, tool_result_records)
        candidate_set_id = batch.candidate_set_id
    else:
        raw_candidates = saved_candidates
        candidate_set_id = saved_candidate_set_id
        if not raw_candidates:
            raise CompletionEnrichmentError(
                "awaiting_selection requires a successful show_architecture_plan batch "
                "and rich detail for every candidate"
            )

    candidates = [
        _normalize_candidate(
            candidate,
            index=index,
            hard_constraints=hard_constraints,
            authoritative_resource_intents=resource_intents,
        )
        for index, candidate in enumerate(raw_candidates)
    ]
    names = [candidate["name"] for candidate in candidates]
    if len(set(names)) != len(names):
        raise CompletionEnrichmentError("candidate names must be unique within one planning batch")

    conclusion: dict[str, Any] = {
        "status": status,
        "continue_pipeline": True,
        "is_infra_intent": True,
        "intent": copy.deepcopy(intent),
        "candidates": candidates,
        "user_prompt": _("Choose the solution to implement and deploy"),
        "options": [
            {
                "name": candidate["name"],
                "summary": _candidate_option_summary(candidate),
                "candidate_index": index,
            }
            for index, candidate in enumerate(candidates)
        ],
    }
    if isinstance(candidate_set_id, str) and candidate_set_id:
        conclusion["candidate_set_id"] = candidate_set_id
    if status == "awaiting_selection":
        tool_input["conclusion"] = conclusion
        return tool_input

    index = raw.get("selected_candidate_index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(candidates):
        raise CompletionEnrichmentError("selected_candidate_index must identify one saved candidate")
    selected = candidates[index]
    conclusion.update(
        {
            "selected_candidate_index": index,
            "selected_candidate_name": selected["name"],
            "selected_candidate": copy.deepcopy(selected),
            "user_input": user_message,
        }
    )
    tool_input["conclusion"] = conclusion
    return tool_input


def _candidates_from_current_batch(
    batch: CandidateOutlineBatch,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_details = latest_candidate_detail_records(records, batch)
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    for index, outline in enumerate(batch.candidates):
        record = latest_details.get(index)
        if record is None:
            errors.append(
                _("candidate {index} {name!r} is missing show_candidate_detail").format(
                    index=index, name=outline["candidate_name"]
                )
            )
            continue
        if record.get("is_error"):
            summary = str(record.get("error_summary") or _("latest detail call failed")).strip()
            errors.append(
                _("candidate {index} {name!r} detail failed: {summary}").format(
                    index=index, name=outline["candidate_name"], summary=summary
                )
            )
            continue
        if not detail_record_matches(record, index=index, candidate_name=outline["candidate_name"]):
            errors.append(
                _("candidate {index} detail must use candidate_name {name!r} from the active batch").format(
                    index=index, name=outline["candidate_name"]
                )
            )
            continue
        detail = record.get("input")
        if not isinstance(detail, dict):
            errors.append(_("candidate {index} detail input is unavailable").format(index=index))
            continue
        candidates.append(_candidate_from_outline_and_detail(outline, detail))

    for index in sorted(latest_details):
        if index >= len(batch.candidates) and not latest_details[index].get("is_error"):
            errors.append(
                _("candidate detail index {index} is outside active batch range 0..{last_index}").format(
                    index=index, last_index=len(batch.candidates) - 1
                )
            )
    if errors:
        shown = errors[:5]
        suffix = (
            _("; {count} more error(s) omitted").format(count=len(errors) - len(shown))
            if len(errors) > len(shown)
            else ""
        )
        raise CompletionEnrichmentError(
            _("complete_step is blocked until the active candidate batch is fully detailed: {errors}{suffix}").format(
                errors="; ".join(shown), suffix=suffix
            )
        )
    return candidates


def _candidate_from_outline_and_detail(
    outline: dict[str, str],
    detail: dict[str, Any],
) -> dict[str, Any]:
    inventory = copy.deepcopy(detail.get("resource_inventory"))
    inventory = inventory if isinstance(inventory, list) else []
    return {
        "name": outline["candidate_name"],
        "summary": outline["summary"],
        "applicable_scenarios": copy.deepcopy(detail.get("applicable_scenarios") or []),
        "resource_intents": copy.deepcopy(detail.get("resource_intents") or []),
        "topology_graph": copy.deepcopy(detail.get("topology_graph") or {}),
        "resource_inventory": inventory,
        "rough_cost": {
            "currency": "CNY",
            "monthly_range": outline["total_monthly_cost"],
            "items": _cost_items_from_inventory(inventory),
            "assumptions": copy.deepcopy(detail.get("cost_assumptions") or []),
            "exclusions": copy.deepcopy(detail.get("cost_exclusions") or []),
            "confidence": detail.get("cost_confidence"),
        },
        "decision_notes": copy.deepcopy(detail.get("decision_notes") or {}),
    }


def _cost_items_from_inventory(inventory: list[Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for raw in inventory:
        if not isinstance(raw, dict):
            continue
        spec = str(raw.get("recommended_spec") or "").strip()
        quantity = raw.get("quantity")
        if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 1:
            spec = f"{spec} × {quantity}" if spec else f"× {quantity}"
        items.append(
            {
                "name": str(raw.get("product") or raw.get("resource_id") or "").strip(),
                "spec": spec,
                "monthly_cost": str(raw.get("rough_monthly_cost") or "").strip(),
            }
        )
    return items


def _normalize_candidate(
    candidate: Any,
    *,
    index: int,
    hard_constraints: list[dict[str, Any]],
    authoritative_resource_intents: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise CompletionEnrichmentError(_("candidates[{index}] must be an object").format(index=index))
    name = candidate.get("name")
    summary = candidate.get("summary")
    if not isinstance(name, str) or not name.strip():
        raise CompletionEnrichmentError(_("candidates[{index}].name must be non-empty").format(index=index))
    if not isinstance(summary, str) or not summary.strip():
        raise CompletionEnrichmentError(_("candidates[{index}].summary must be non-empty").format(index=index))

    normalized = copy.deepcopy(candidate)
    normalized["candidate_id"] = f"candidate-{index}"
    normalized["name"] = name.strip()
    normalized["summary"] = summary.strip()
    _validate_candidate_resource_intents(
        normalized.get("resource_intents"),
        authoritative_resource_intents,
        candidate_index=index,
    )
    normalized["output_path"] = f"templates/{index}-{_candidate_slug(name)}.yml"
    normalized["hard_constraints"] = copy.deepcopy(hard_constraints)
    normalized["products"] = _candidate_products(normalized)
    normalized["topology"] = _topology_text(normalized)

    notes = normalized.pop("decision_notes", {})
    notes = notes if isinstance(notes, dict) else {}
    for field in ("why_recommended", "problems_solved", "pros", "cons", "risks", "tradeoffs"):
        value = notes.get(field, normalized.get(field, []))
        normalized[field] = copy.deepcopy(value) if isinstance(value, list) else []
    for field, minimum in _REQUIRED_DECISION_NOTES:
        entries = [text.strip() for text in normalized[field] if isinstance(text, str) and text.strip()]
        if len(entries) < minimum:
            raise CompletionEnrichmentError(
                _(
                    "candidates[{index}].decision_notes.{field} must list at least "
                    "{minimum} non-empty entries tied to this candidate's architecture"
                ).format(index=index, field=field, minimum=minimum)
            )
        normalized[field] = entries
    normalized.setdefault("applicable_scenarios", [])
    return normalized


def _validate_candidate_resource_intents(
    candidate_value: Any,
    authoritative: list[dict[str, Any]],
    *,
    candidate_index: int,
) -> None:
    if not isinstance(candidate_value, list) or not all(isinstance(item, dict) for item in candidate_value):
        raise CompletionEnrichmentError(
            _("candidates[{candidate_index}].resource_intents must be an array of objects").format(
                candidate_index=candidate_index
            )
        )

    candidate_actions: dict[str, set[str]] = {}
    for item in candidate_value:
        product = str(item.get("product") or "").strip().casefold()
        action = str(item.get("action") or "").strip().casefold()
        if product and action:
            candidate_actions.setdefault(product, set()).add(action)

    missing: list[str] = []
    for item in authoritative:
        product = str(item.get("product") or "").strip()
        action = str(item.get("action") or "").strip().casefold()
        source = str(item.get("source") or "").strip().casefold()
        # Inferred optional resources may legitimately differ between candidates. User-authored
        # lifecycle decisions and every non-create restriction are authoritative for all of them.
        required = source not in {"inferred", "predefined_solution"} or action != "create"
        if not required or not product or action not in {"create", "use_existing", "reference", "forbid"}:
            continue
        if action not in candidate_actions.get(product.casefold(), set()):
            missing.append(f"{product}:{action}")

    if missing:
        shown = missing[:5]
        suffix = (
            _("; {count} more omitted").format(count=len(missing) - len(shown))
            if len(missing) > len(shown)
            else ""
        )
        raise CompletionEnrichmentError(
            _(
                "candidates[{candidate_index}].resource_intents must preserve authoritative intent lifecycle: "
                "{missing}{suffix}; submit a corrected candidate batch and details"
            ).format(candidate_index=candidate_index, missing=", ".join(shown), suffix=suffix)
        )


def _candidate_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return (slug[:48].rstrip("-") or "solution")


def _candidate_products(candidate: dict[str, Any]) -> list[str]:
    products: list[str] = []
    collections = (
        (candidate.get("resource_inventory"), "product"),
        (candidate.get("resource_intents"), "product"),
    )
    for collection, field in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            value = item.get(field) if isinstance(item, dict) else None
            if isinstance(value, str) and value and value not in products:
                products.append(value)
    graph = candidate.get("topology_graph")
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            value = node.get("product") if isinstance(node, dict) else None
            if isinstance(value, str) and value and value not in products:
                products.append(value)
    return products


def _topology_text(candidate: dict[str, Any]) -> str:
    graph = candidate.get("topology_graph")
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        labels = [str(node.get("label")) for node in nodes if isinstance(node, dict) and node.get("label")]
        if labels:
            return " → ".join(labels)
    return str(candidate.get("summary") or "")


def _candidate_option_summary(candidate: dict[str, Any]) -> str:
    """Compose 「概述；月度价格；首要代价」, idempotently.

    重新规划时模型在自己的上下文里看到的是上一轮拼好的选项文案，会把它原样当成候选概述
    交回来，于是价格与代价在方案卡里出现两遍。价格前的分隔符是上一轮拼接留下的标记，
    从它开始整条旧尾巴都可以丢掉；价格变了的情况下退回逐段去重，至少不再重复同一句话。
    """
    summary = str(candidate.get("summary") or "")
    rough_cost = candidate.get("rough_cost")
    monthly_range = rough_cost.get("monthly_range") if isinstance(rough_cost, dict) else None
    notes = candidate.get("cons")
    price = str(monthly_range or "")
    tradeoff = str(notes[0]) if isinstance(notes, list) and notes else ""
    summary = _strip_composed_option_tail(summary, current_price=price)
    summary = summary.strip()
    parts = [summary] if summary else []
    parts.extend(part for part in (price, tradeoff) if part and part not in summary)
    return "；".join(parts)


def _strip_composed_option_tail(summary: str, *, current_price: str) -> str:
    """Remove an option suffix echoed back as the next candidate's semantic summary."""

    if current_price:
        composed_at = summary.find(f"；{current_price}")
        if composed_at >= 0:
            return summary[:composed_at]

    segments = summary.split("；")
    # A projected option always has content before the price and a trade-off after it.
    # Requiring a complete price-shaped segment avoids trimming ordinary prose that uses semicolons.
    for index in range(1, len(segments) - 1):
        if _COMPOSED_MONTHLY_PRICE_RE.fullmatch(segments[index]):
            return "；".join(segments[:index])
    return summary
