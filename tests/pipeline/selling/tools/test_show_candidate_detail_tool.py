import asyncio

import pytest

from iac_code.pipeline.selling.tools.show_candidate_detail_tool import ShowCandidateDetailTool
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import CandidateDetailEvent


class TestShowCandidateDetailToolMeta:
    def test_name(self):
        tool = ShowCandidateDetailTool()
        assert tool.name == "show_candidate_detail"

    def test_is_read_only(self):
        tool = ShowCandidateDetailTool()
        assert tool.is_read_only() is True

    def test_needs_event_queue(self):
        tool = ShowCandidateDetailTool()
        assert tool.needs_event_queue() is True

    def test_input_schema_has_required_fields(self):
        tool = ShowCandidateDetailTool()
        schema = tool.input_schema
        assert "candidate_name" in schema["properties"]
        assert "summary" in schema["properties"]
        assert "cost_items" in schema["properties"]
        assert "total_monthly_cost" in schema["properties"]
        assert "candidate_index" in schema["properties"]
        assert set(schema["required"]) == {
            "candidate_name",
            "candidate_index",
            "summary",
            "cost_items",
            "total_monthly_cost",
        }


class TestShowCandidateDetailToolExecute:
    @pytest.mark.asyncio
    async def test_emits_candidate_detail_event(self):
        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(cwd="/tmp", event_queue=queue)
        tool = ShowCandidateDetailTool()

        result = await tool.execute(
            tool_input={
                "candidate_name": "简单方案",
                "summary": "单台ECS部署Nginx",
                "cost_items": [{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50/月"}],
                "total_monthly_cost": "¥50/月",
            },
            context=context,
        )

        assert not result.is_error
        assert not queue.empty()
        event = queue.get_nowait()
        assert isinstance(event, CandidateDetailEvent)
        assert event.candidate_name == "简单方案"
        assert event.summary == "单台ECS部署Nginx"
        assert len(event.cost_items) == 1
        assert event.total_monthly_cost == "¥50/月"

    @pytest.mark.asyncio
    async def test_show_candidate_detail_emits_candidate_index(self):
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowCandidateDetailTool()

        result = await tool.execute(
            tool_input={
                "candidate_name": "Same",
                "candidate_index": 1,
                "summary": "summary",
                "cost_items": [],
                "total_monthly_cost": "¥0/月",
            },
            context=ToolContext(cwd=".", event_queue=queue, tool_use_id="tu_1"),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, CandidateDetailEvent)
        assert event.candidate_name == "Same"
        assert event.candidate_index == 1

    @pytest.mark.asyncio
    async def test_no_event_queue(self):
        context = ToolContext(cwd="/tmp", event_queue=None)
        tool = ShowCandidateDetailTool()

        result = await tool.execute(
            tool_input={
                "candidate_name": "方案1",
                "summary": "desc",
                "cost_items": [],
                "total_monthly_cost": "¥0",
            },
            context=context,
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_emitted_event_carries_tool_use_id_from_context(self):
        """U-I14: emitter must propagate ToolContext.tool_use_id onto the event."""
        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(cwd="/tmp", event_queue=queue, tool_use_id="tu_xyz")
        tool = ShowCandidateDetailTool()

        result = await tool.execute(
            tool_input={
                "candidate_name": "方案A",
                "summary": "s",
                "cost_items": [],
                "total_monthly_cost": "¥0",
            },
            context=context,
        )
        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, CandidateDetailEvent)
        assert event.tool_use_id == "tu_xyz"
