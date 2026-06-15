"""Tests for MetricsRegistry."""

from unittest.mock import MagicMock

from iac_code.services.telemetry.metrics import METRIC_NAMES, MetricsRegistry
from iac_code.services.telemetry.names import Metrics as M  # noqa: N817


def test_metric_names_covers_spec_core_set():
    expected = {
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
    }
    assert expected.issubset(set(METRIC_NAMES))


def test_metric_names_include_pipeline_metrics():
    expected = {
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
    }
    assert expected.issubset(set(METRIC_NAMES))


def test_add_dispatches_to_counter():
    counter = MagicMock()
    registry = MetricsRegistry(instruments={M.SESSION_COUNT: counter})
    registry.add(M.SESSION_COUNT, 1, {"os.type": "linux"})
    counter.add.assert_called_once_with(1, {"os.type": "linux"})


def test_add_dispatches_to_histogram():
    hist = MagicMock()
    registry = MetricsRegistry(instruments={M.API_REQUEST_DURATION: hist})
    registry.add(M.API_REQUEST_DURATION, 123, {"provider": "anthropic"})
    hist.record.assert_called_once_with(123, {"provider": "anthropic"})


def test_add_unknown_name_is_noop():
    registry = MetricsRegistry()
    registry.add("iac.does.not.exist", 1, {})  # must not raise


def test_register_all_creates_counter_and_histogram_instruments():
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock(name="counter-inst")
    meter.create_histogram.return_value = MagicMock(name="histogram-inst")
    registry = MetricsRegistry()
    registry.register_all(meter)
    assert meter.create_counter.called
    assert meter.create_histogram.called
    # At least the three known histograms were created
    hist_names = {call.kwargs.get("name") or call.args[0] for call in meter.create_histogram.call_args_list}
    assert M.API_REQUEST_DURATION in hist_names
    assert M.DEPLOYMENT_DURATION in hist_names
    assert M.ALIYUN_API_CALLED_DURATION in hist_names


def test_pipeline_duration_metrics_are_histograms():
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock(name="counter-inst")
    meter.create_histogram.return_value = MagicMock(name="histogram-inst")
    registry = MetricsRegistry()
    registry.register_all(meter)

    hist_names = {call.kwargs.get("name") or call.args[0] for call in meter.create_histogram.call_args_list}
    assert M.PIPELINE_STEP_DURATION in hist_names
    assert M.PIPELINE_COMPLETION_TIME in hist_names
    assert M.PIPELINE_SUB_PIPELINE_DURATION in hist_names
    assert M.PIPELINE_SUB_STEP_DURATION in hist_names
    assert M.PIPELINE_USER_INPUT_WAIT_DURATION in hist_names


def test_pipeline_count_metrics_are_counters():
    meter = MagicMock()
    meter.create_counter.return_value = MagicMock(name="counter-inst")
    meter.create_histogram.return_value = MagicMock(name="histogram-inst")
    registry = MetricsRegistry()
    registry.register_all(meter)

    counter_names = {call.kwargs.get("name") or call.args[0] for call in meter.create_counter.call_args_list}
    assert M.PIPELINE_ROLLBACK_COUNT in counter_names
    assert M.PIPELINE_CANDIDATE_CANCELLED_COUNT in counter_names
    assert M.PIPELINE_CANDIDATE_COUNT in counter_names
    assert M.PIPELINE_CANDIDATE_SUCCESS_COUNT in counter_names
    assert M.PIPELINE_CANDIDATE_FAILED_COUNT in counter_names
    assert M.PIPELINE_FUNNEL_STEP_COUNT in counter_names
