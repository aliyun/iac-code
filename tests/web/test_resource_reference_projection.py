"""Exercise canonical tool facts through normal and pipeline replay paths."""

from iac_code.a2a.pipeline_events import _tool_result_data
from iac_code.agent.message import ToolResultBlock
from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import RESOURCE_REFERENCES_METADATA_KEY, resource_reference_metadata
from iac_code.types.stream_events import ToolResultEvent
from iac_code.web.events import WebEventTranslator
from iac_code.web.pipeline_transcript import PipelineTranscriptTranslator, build_pipeline_transcript_rows
from iac_code.web.session_manager import _tool_result_payload


def test_resource_facts_have_equal_normal_live_history_and_pipeline_projections():
    content = '{"Role":{"RoleName":"DeployRole"}}'
    references = extract_api_resource_references(
        product="ram",
        action="GetRole",
        params={"RoleName": "DeployRole"},
        body={"Role": {"RoleName": "DeployRole"}},
        region_id="cn-hangzhou",
        tool_use_id="tool-1",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    metadata = {RESOURCE_REFERENCES_METADATA_KEY: resource_reference_metadata(references)}
    event = ToolResultEvent(tool_use_id="tool-1", tool_name="aliyun_api", result=content, metadata=metadata)
    live = WebEventTranslator("session-1").translate_stream_event(event, turn_id="turn-1")["payload"]
    assert live["artifacts"] == []
    assert live["summary"] == content
    saved = _tool_result_payload(ToolResultBlock(tool_use_id="tool-1", content=content, metadata=metadata))
    assert saved["resourceReferences"] == live["resourceReferences"]
    envelope = {
        "eventType": "tool_result",
        "scope": "step",
        "sequence": 1,
        "step": {"id": "deploy", "runId": "deploy-1", "index": 1, "total": 1},
        "data": _tool_result_data(event),
    }
    projected = PipelineTranscriptTranslator().translate_all([envelope])
    pipeline_live = next(item["payload"] for item in projected if item["type"] == "tool.result")
    assert pipeline_live["resourceReferences"] == live["resourceReferences"]
    rows = build_pipeline_transcript_rows([envelope])
    pipeline_saved = rows[0]["tools"]["tool-1"]["results"][0]
    assert pipeline_saved["resourceReferences"] == live["resourceReferences"]
    assert pipeline_saved["content"] == content
    assert all("regionId" not in item for item in pipeline_saved["resourceReferences"])
