from iac_code.services.telemetry.names import IacCodeAttr, PipelineAttr
from iac_code.services.telemetry.scope import (
    get_span_attributes,
    normalize_span_attributes,
    replace_span_attributes,
    use_span_attributes,
)


def test_scope_normalization_keeps_only_bounded_dimensions() -> None:
    assert normalize_span_attributes(
        {
            IacCodeAttr.MODE: "pipeline",
            PipelineAttr.STEP_ID: "intent_parsing",
            PipelineAttr.CANDIDATE_INDEX: 2,
            PipelineAttr.SUB_STEP_ID: "x" * 257,
            "prompt": "customer content",
            "unknown": True,
        }
    ) == {
        IacCodeAttr.MODE: "pipeline",
        PipelineAttr.STEP_ID: "intent_parsing",
        PipelineAttr.CANDIDATE_INDEX: 2,
    }


def test_scope_nesting_merges_and_restores_dimensions() -> None:
    assert get_span_attributes() == {}


def test_scope_replacement_does_not_merge_and_restores_original_dimensions() -> None:
    with use_span_attributes({IacCodeAttr.MODE: "normal", PipelineAttr.RUN_ID: "run-b"}):
        with replace_span_attributes({IacCodeAttr.MODE: "pipeline", PipelineAttr.STEP_ID: "step-a"}):
            assert get_span_attributes() == {
                IacCodeAttr.MODE: "pipeline",
                PipelineAttr.STEP_ID: "step-a",
            }
        assert get_span_attributes() == {
            IacCodeAttr.MODE: "normal",
            PipelineAttr.RUN_ID: "run-b",
        }

    with use_span_attributes({IacCodeAttr.MODE: "pipeline", PipelineAttr.STEP_ID: "parent"}):
        assert get_span_attributes() == {
            IacCodeAttr.MODE: "pipeline",
            PipelineAttr.STEP_ID: "parent",
        }
        with use_span_attributes({PipelineAttr.STEP_ID: "child"}):
            assert get_span_attributes() == {
                IacCodeAttr.MODE: "pipeline",
                PipelineAttr.STEP_ID: "child",
            }
        assert get_span_attributes()[PipelineAttr.STEP_ID] == "parent"

    assert get_span_attributes() == {}
