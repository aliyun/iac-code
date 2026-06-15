import time
import typing

from iac_code.pipeline.engine.events import PipelineEvent, PipelineEventType
from iac_code.types.stream_events import DiagramEvent, StreamEvent


class TestPipelineEventType:
    def test_all_values_are_strings(self):
        for member in PipelineEventType:
            assert isinstance(member, str)

    def test_key_events_exist(self):
        assert PipelineEventType.PIPELINE_STARTED == "pipeline_started"
        assert PipelineEventType.STEP_STARTED == "step_started"
        assert PipelineEventType.USER_INPUT_REQUIRED == "user_input_required"
        assert PipelineEventType.ROLLBACK_TRIGGERED == "rollback_triggered"


class TestPipelineEvent:
    def test_construction(self):
        event = PipelineEvent(
            type=PipelineEventType.STEP_STARTED,
            step_id="intent_parsing",
            timestamp=time.time(),
            data={"index": 1, "total": 8, "name": "意图解析"},
        )
        assert event.step_id == "intent_parsing"
        assert event.data["index"] == 1

    def test_pipeline_level_event_no_step(self):
        event = PipelineEvent(
            type=PipelineEventType.PIPELINE_STARTED,
            step_id=None,
            timestamp=time.time(),
            data={"pipeline_type": "selling"},
        )
        assert event.step_id is None


class TestSubPipelineEventTypes:
    def test_sub_pipeline_started_exists(self):
        assert PipelineEventType.SUB_PIPELINE_STARTED == "sub_pipeline_started"

    def test_sub_pipeline_completed_exists(self):
        assert PipelineEventType.SUB_PIPELINE_COMPLETED == "sub_pipeline_completed"

    def test_sub_step_started_exists(self):
        assert PipelineEventType.SUB_STEP_STARTED == "sub_step_started"

    def test_sub_step_completed_exists(self):
        assert PipelineEventType.SUB_STEP_COMPLETED == "sub_step_completed"


class TestInterruptEventTypes:
    def test_interrupted_exists(self):
        assert PipelineEventType.INTERRUPTED == "interrupted"

    def test_candidate_interrupted_exists(self):
        assert PipelineEventType.CANDIDATE_INTERRUPTED == "candidate_interrupted"


class TestDiagramEvent:
    def test_fields(self):
        event = DiagramEvent(
            candidate_name="简单Nginx方案",
            template_content="ROSTemplateFormatVersion: '2015-09-01'\nResources: {}",
            mermaid_source="graph TD\n  VPC --> VSwitch",
        )
        assert event.candidate_name == "简单Nginx方案"
        assert event.template_content == "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}"
        assert event.mermaid_source == "graph TD\n  VPC --> VSwitch"
        assert event.type == "diagram"

    def test_in_stream_event_union(self):
        """DiagramEvent must be part of the StreamEvent union type."""
        args = typing.get_args(StreamEvent)
        assert DiagramEvent in args
