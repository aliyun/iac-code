"""MetricsRegistry — wraps OTel Counter/Histogram instruments."""

from __future__ import annotations

from typing import Any

from loguru import logger
from opentelemetry.metrics import Meter

from iac_code.services.telemetry.names import Metrics as M  # noqa: N817

METRIC_NAMES: tuple[str, ...] = (
    M.SESSION_COUNT,
    M.ACTIVE_TIME_TOTAL,
    M.TOKEN_USAGE,
    M.API_REQUEST_COUNT,
    M.API_REQUEST_DURATION,
    M.TOOL_USE_COUNT,
    M.TEMPLATE_GENERATED_COUNT,
    M.TEMPLATE_VALIDATED_COUNT,
    M.DEPLOYMENT_COUNT,
    M.DEPLOYMENT_DURATION,
    M.RESOURCE_TYPE_OBSERVED_COUNT,
    M.ALIYUN_API_CALLED_COUNT,
    M.ALIYUN_API_CALLED_DURATION,
    M.TERRAFORM_PROVIDER_OBSERVED_COUNT,
    M.PIPELINE_STEP_DURATION,
    M.PIPELINE_ROLLBACK_COUNT,
    M.PIPELINE_COMPLETION_TIME,
    M.PIPELINE_SUB_PIPELINE_DURATION,
    M.PIPELINE_SUB_STEP_DURATION,
    M.PIPELINE_CANDIDATE_CANCELLED_COUNT,
    M.PIPELINE_USER_INPUT_WAIT_DURATION,
    M.PIPELINE_CANDIDATE_COUNT,
    M.PIPELINE_CANDIDATE_SUCCESS_COUNT,
    M.PIPELINE_CANDIDATE_FAILED_COUNT,
    M.PIPELINE_FUNNEL_STEP_COUNT,
)

_HISTOGRAM_NAMES: frozenset[str] = frozenset(
    {
        M.API_REQUEST_DURATION,
        M.DEPLOYMENT_DURATION,
        M.ALIYUN_API_CALLED_DURATION,
        M.PIPELINE_STEP_DURATION,
        M.PIPELINE_COMPLETION_TIME,
        M.PIPELINE_SUB_PIPELINE_DURATION,
        M.PIPELINE_SUB_STEP_DURATION,
        M.PIPELINE_USER_INPUT_WAIT_DURATION,
    }
)


class MetricsRegistry:
    """Holds instrument objects and dispatches add/record calls."""

    def __init__(self, instruments: dict[str, Any] | None = None) -> None:
        self._instruments: dict[str, Any] = dict(instruments) if instruments else {}

    def register_all(self, meter: Meter) -> None:
        """Create a Counter or Histogram per known metric name."""
        self._instruments.clear()
        for name in METRIC_NAMES:
            if name in _HISTOGRAM_NAMES:
                self._instruments[name] = meter.create_histogram(name=name, description=name)
            else:
                self._instruments[name] = meter.create_counter(name=name, description=name)

    def add(self, name: str, value: int | float, attributes: dict[str, Any]) -> None:
        """Route to the correct instrument method; silently drop unknown names."""
        logger.info("[metric] {} value={} {}", name, value, attributes)
        inst = self._instruments.get(name)
        if inst is None:
            return
        if name in _HISTOGRAM_NAMES:
            inst.record(value, attributes)
        else:
            inst.add(value, attributes)
