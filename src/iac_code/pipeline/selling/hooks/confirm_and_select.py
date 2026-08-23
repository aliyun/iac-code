"""Hook for the confirm_and_select step.

Adds a deterministic cross-step cost-consistency check before the user
confirms a plan. The architecture planning step emits a rough monthly cost
range (``candidate.monthly_estimate``) while the ``cost_estimating`` sub-step
produces the authoritative price from ``ros_estimate_template_cost``
(``cost.monthly_estimate``). When the two diverge beyond a threshold the user
was previously confirming on the under-estimated planning figure. This hook
annotates each evaluated candidate with a structured ``cost_consistency``
result so the confirmation surfaces can restate the real monthly cost and gate
deployment behind an explicit second confirmation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from iac_code.pipeline.engine.context import PipelineContext

logger = logging.getLogger(__name__)

_DEFAULT_DEVIATION_THRESHOLD = 1.5
_THRESHOLD_ENV_VAR = "IAC_CODE_SELLING_COST_DEVIATION_THRESHOLD"

# Matches monetary amounts such as ``80``, ``120.5``, ``1,234.00``.
_AMOUNT_PATTERN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _resolve_threshold() -> float:
    raw = os.environ.get(_THRESHOLD_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_DEVIATION_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to default", _THRESHOLD_ENV_VAR, raw)
        return _DEFAULT_DEVIATION_THRESHOLD
    if value <= 1:
        logger.warning("%s=%r must be > 1; falling back to default", _THRESHOLD_ENV_VAR, raw)
        return _DEFAULT_DEVIATION_THRESHOLD
    return value


def _parse_amounts(value: Any) -> list[float]:
    """Extract all monetary amounts from a cost string in order of appearance."""
    if not isinstance(value, str):
        return []
    amounts: list[float] = []
    for match in _AMOUNT_PATTERN.finditer(value):
        try:
            amounts.append(float(match.group(0).replace(",", "")))
        except ValueError:
            continue
    return amounts


def parse_planning_estimate(value: Any) -> float | None:
    """Parse the planning rough estimate; use the upper bound of any range.

    The architecture planning step emits values like ``¥80-120/月`` or
    ``¥100/月``. The upper bound is the most optimistic-friendly comparison
    basis (a real price above the highest planned figure is unambiguously an
    under-estimate).
    """
    amounts = _parse_amounts(value)
    if not amounts:
        return None
    return max(amounts)


def parse_actual_estimate(value: Any) -> float | None:
    """Parse the authoritative price; use the list price (first amount).

    ``cost_estimating`` returns strings such as ``¥289.81/月`` or
    ``¥96.80/月（列表价，合同优惠后约¥13.76/月）``. The first amount is the list
    price, which is the same basis the confirmation surfaces display.
    """
    amounts = _parse_amounts(value)
    if not amounts:
        return None
    return amounts[0]


def evaluate_cost_consistency(
    planning_estimate: Any,
    actual_estimate: Any,
    *,
    threshold: float | None = None,
) -> dict[str, Any] | None:
    """Compare the planning estimate with the actual price.

    Returns a structured result, or ``None`` when either figure cannot be
    parsed into a positive number (e.g. pricing failed).
    """
    planning = parse_planning_estimate(planning_estimate)
    actual = parse_actual_estimate(actual_estimate)
    if planning is None or actual is None:
        return None
    if planning <= 0 or actual <= 0:
        return None

    resolved_threshold = threshold if threshold is not None else _resolve_threshold()
    deviation_ratio = round(actual / planning, 2)
    exceeds_threshold = deviation_ratio >= resolved_threshold or (1 / deviation_ratio) >= resolved_threshold

    result: dict[str, Any] = {
        "planning_estimate": planning_estimate,
        "actual_estimate": actual_estimate,
        "planning_amount": planning,
        "actual_amount": actual,
        "deviation_ratio": deviation_ratio,
        "threshold": resolved_threshold,
        "exceeds_threshold": exceeds_threshold,
    }
    if exceeds_threshold:
        result["message"] = (
            "候选实际询价月费约 {actual} 与架构规划预估约 {planning} 偏差约 {ratio} 倍，"
            "已超过一致性阈值 {threshold} 倍；确认部署前必须以实际询价为准并二次确认。"
        ).format(
            actual=actual_estimate,
            planning=planning_estimate,
            ratio=deviation_ratio,
            threshold=resolved_threshold,
        )
    return result


def annotate_cost_consistency(
    evaluated_candidates: Any,
    *,
    threshold: float | None = None,
) -> bool:
    """Annotate each non-failed candidate in place with ``cost_consistency``.

    Returns whether any candidate exceeded the deviation threshold.
    """
    if not isinstance(evaluated_candidates, list):
        return False

    any_exceeds = False
    for result in evaluated_candidates:
        if not isinstance(result, dict) or result.get("failed"):
            continue
        candidate = result.get("candidate")
        cost = result.get("cost")
        if not isinstance(candidate, dict) or not isinstance(cost, dict):
            continue
        consistency = evaluate_cost_consistency(
            candidate.get("monthly_estimate"),
            cost.get("monthly_estimate"),
            threshold=threshold,
        )
        if consistency is None:
            result.pop("cost_consistency", None)
            continue
        result["cost_consistency"] = consistency
        if consistency.get("exceeds_threshold"):
            any_exceeds = True
    return any_exceeds


def on_enter(ctx: PipelineContext) -> None:
    """Annotate evaluated candidates with cross-step cost consistency."""
    evaluated_candidates = ctx.get_conclusion("evaluated_candidates")
    annotate_cost_consistency(evaluated_candidates)
