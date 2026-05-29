import pytest

from iac_code.a2a.exposure import A2AExposureType, normalize_a2a_exposure_types


def test_normalize_a2a_exposure_types_defaults_to_tool_trace() -> None:
    assert normalize_a2a_exposure_types(None) == frozenset({A2AExposureType.TOOL_TRACE})


def test_normalize_a2a_exposure_types_accepts_multi_select_lists_and_aliases() -> None:
    assert normalize_a2a_exposure_types(["raw-thinking", "tool_trace"]) == frozenset(
        {A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE}
    )


def test_normalize_a2a_exposure_types_accepts_comma_separated_string() -> None:
    assert normalize_a2a_exposure_types("raw-thinking, tool-trace") == frozenset(
        {A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE}
    )


def test_normalize_a2a_exposure_types_rejects_unsupported_types() -> None:
    with pytest.raises(ValueError, match="Unsupported A2A thinking exposure type"):
        normalize_a2a_exposure_types(["thought-summary"])
