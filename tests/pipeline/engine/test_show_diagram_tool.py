import asyncio

import pytest

from iac_code.pipeline.engine import show_diagram_tool
from iac_code.pipeline.engine.show_diagram_tool import ShowArchitectureDiagramTool, ros_template_to_mermaid
from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.tools.tool_executor import ToolCallRequest, ToolExecutor
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent, ToolEmittedEvent


def _assert_error_diagram_event(queue: asyncio.Queue, *, candidate_name: str, expected_text: str) -> None:
    event = queue.get_nowait()
    assert isinstance(event, DiagramEvent)
    assert event.candidate_name == candidate_name
    assert event.diagram_stage == "optimized"
    assert expected_text in event.mermaid_source
    assert event.views
    assert expected_text in event.views[0]["mermaid_source"]


SIMPLE_TEMPLATE = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 172.16.0.0/12
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
      CidrBlock: 172.16.0.0/24
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  ECSInstance:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
"""

SLB_TEMPLATE = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 172.16.0.0/12
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS1:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  ECS2:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  SLB:
    Type: ALIYUN::SLB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC
  BackendAttachment:
    Type: ALIYUN::SLB::BackendServerAttachment
    Properties:
      LoadBalancerId:
        Ref: SLB
      BackendServers:
        - Fn::GetAtt:
            - ECS1
            - InstanceId
        - Fn::GetAtt:
            - ECS2
            - InstanceId
  RDS:
    Type: ALIYUN::RDS::DBInstance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

EIP_TEMPLATE = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.0.0.0/8
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  EIP:
    Type: ALIYUN::VPC::EIP
    Properties:
      BandWidth: 5
  EIPAssociation:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ECS
"""


def _many_resource_template(count: int = 80) -> str:
    resources = [
        "ROSTemplateFormatVersion: '2015-09-01'",
        "Resources:",
        "  VPC:",
        "    Type: ALIYUN::ECS::VPC",
        "    Properties:",
        "      CidrBlock: 10.0.0.0/8",
        "  VSwitch:",
        "    Type: ALIYUN::ECS::VSwitch",
        "    Properties:",
        "      VpcId:",
        "        Ref: VPC",
        "      CidrBlock: 10.0.0.0/24",
    ]
    for index in range(count):
        resources.extend(
            [
                f"  ECS{index}:",
                "    Type: ALIYUN::ECS::Instance",
                "    Properties:",
                "      VSwitchId:",
                "        Ref: VSwitch",
            ]
        )
    return "\n".join(resources) + "\n"


class TestToolEmittedEvent:
    def test_diagram_event_is_tool_emitted(self):
        event = DiagramEvent(
            candidate_name="test",
            template_content="yaml",
            mermaid_source="graph TD",
        )
        assert isinstance(event, ToolEmittedEvent)


class TestCandidateDetailEvent:
    def test_is_tool_emitted(self):
        event = CandidateDetailEvent(
            tool_use_id="test_tu_1",
            candidate_name="方案1",
            summary="简单Nginx方案",
            cost_items=[{"name": "ECS", "spec": "1C2G", "monthly_cost": "¥50/月"}],
            total_monthly_cost="¥50/月",
        )
        assert isinstance(event, ToolEmittedEvent)
        assert isinstance(event, CandidateDetailEvent)

    def test_fields(self):
        event = CandidateDetailEvent(
            tool_use_id="test_tu_2",
            candidate_name="方案1",
            summary="简单方案",
            cost_items=[],
            total_monthly_cost="¥0",
        )
        assert event.candidate_name == "方案1"
        assert event.summary == "简单方案"
        assert event.cost_items == []
        assert event.total_monthly_cost == "¥0"
        assert event.type == "candidate_detail"


class TestToolBaseNeedsEventQueue:
    def test_default_is_false(self):
        class DummyTool(Tool):
            @property
            def name(self):
                return "dummy"

            @property
            def description(self):
                return "dummy"

            @property
            def input_schema(self):
                return {"type": "object", "properties": {}}

            async def execute(self, *, tool_input, context):
                return ToolResult.success("ok")

        assert DummyTool().needs_event_queue() is False


class TestRosTemplateToMermaid:
    def test_layers_rendered_as_subgraphs(self):
        mermaid = ros_template_to_mermaid(SIMPLE_TEMPLATE)
        assert "graph TD" in mermaid
        assert "subgraph layer_VPC [VPC (172.16.0.0/12)]" in mermaid
        assert "subgraph layer_VSwitch [VSwitch (172.16.0.0/24)]" in mermaid
        assert "subgraph layer_SecurityGroup [Security group]" in mermaid

    def test_node_inside_security_group(self):
        mermaid = ros_template_to_mermaid(SIMPLE_TEMPLATE)
        assert 'ECSInstance["ECS instance"]' in mermaid

    def test_vswitch_nested_inside_vpc(self):
        mermaid = ros_template_to_mermaid(SIMPLE_TEMPLATE)
        vpc_pos = mermaid.index("layer_VPC")
        vs_pos = mermaid.index("layer_VSwitch")
        sg_pos = mermaid.index("layer_SecurityGroup")
        assert vs_pos > vpc_pos
        assert sg_pos > vs_pos

    def test_security_group_dashed_style(self):
        mermaid = ros_template_to_mermaid(SIMPLE_TEMPLATE)
        assert "stroke-dasharray: 5 5" in mermaid
        assert "layer_SecurityGroup" in mermaid

    def test_hidden_resources_not_rendered(self):
        mermaid = ros_template_to_mermaid(SLB_TEMPLATE)
        assert "BackendAttachment" not in mermaid

    def test_slb_edges_from_backend_attachment(self):
        mermaid = ros_template_to_mermaid(SLB_TEMPLATE)
        assert "SLB --> ECS1" in mermaid
        assert "SLB --> ECS2" in mermaid

    def test_eip_association_edge(self):
        mermaid = ros_template_to_mermaid(EIP_TEMPLATE)
        assert "EIP --> ECS" in mermaid
        assert "EIPAssociation" not in mermaid

    def test_eip_outside_vpc(self):
        mermaid = ros_template_to_mermaid(EIP_TEMPLATE)
        lines = mermaid.split("\n")
        eip_line = next(line for line in lines if "EIP[" in line and "subgraph" not in line)
        assert not eip_line.startswith("      ")

    def test_multiple_instances_disambiguated(self):
        mermaid = ros_template_to_mermaid(SLB_TEMPLATE)
        assert "ECS instance 1" in mermaid
        assert "ECS instance 2" in mermaid

    def test_cidr_from_parameter_default(self):
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  VpcCidr:
    Type: String
    Default: 192.168.0.0/16
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock:
        Ref: VpcCidr
"""
        mermaid = ros_template_to_mermaid(template)
        assert "192.168.0.0/16" in mermaid

    def test_empty_resources(self):
        mermaid = ros_template_to_mermaid("ROSTemplateFormatVersion: '2015-09-01'\nResources: {}")
        assert "graph TD" in mermaid

    def test_no_resources_key(self):
        mermaid = ros_template_to_mermaid("ROSTemplateFormatVersion: '2015-09-01'")
        assert "graph TD" in mermaid

    def test_yaml_parse_error(self):
        mermaid = ros_template_to_mermaid("{{invalid yaml")
        assert "Error" in mermaid


class TestShowArchitectureDiagramToolMeta:
    def test_name(self):
        tool = ShowArchitectureDiagramTool()
        assert tool.name == "show_architecture_diagram"

    def test_is_read_only(self):
        tool = ShowArchitectureDiagramTool()
        assert tool.is_read_only() is True

    def test_input_schema_has_required_fields(self):
        tool = ShowArchitectureDiagramTool()
        schema = tool.input_schema
        assert "file_path" in schema["properties"]
        assert "candidate_name" in schema["properties"]
        assert "candidate_index" in schema["properties"]
        assert set(schema["required"]) == {"file_path", "candidate_name", "candidate_index"}

    def test_needs_event_queue(self):
        tool = ShowArchitectureDiagramTool()
        assert tool.needs_event_queue() is True

    def test_timeout_allows_slow_semantic_planning(self):
        tool = ShowArchitectureDiagramTool()

        assert tool.timeout is not None
        assert tool.timeout >= 600.0


class TestShowArchitectureDiagramToolExecute:
    @pytest.mark.asyncio
    async def test_emits_diagram_event(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "1-simple.yml").write_text(SIMPLE_TEMPLATE, encoding="utf-8")

        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(cwd=str(tmp_path), event_queue=queue)
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "templates/1-simple.yml", "candidate_name": "简单方案"},
            context=context,
        )

        assert not result.is_error
        assert not queue.empty()
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.candidate_name == "简单方案"
        assert "graph TD" in event.mermaid_source
        assert "ROSTemplateFormatVersion" in event.template_content

    @pytest.mark.asyncio
    async def test_uses_requested_template_file_path(self, tmp_path):
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        requested_template = SIMPLE_TEMPLATE.replace("172.16.0.0/12", "10.10.0.0/16")
        debug_template = SIMPLE_TEMPLATE.replace("172.16.0.0/12", "192.168.99.0/24")
        (template_dir / "requested.yml").write_text(requested_template, encoding="utf-8")
        (template_dir / "public-network-architecture-design.yml").write_text(debug_template, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={
                "file_path": "templates/requested.yml",
                "candidate_name": "requested candidate",
                "candidate_index": 0,
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.template_content == requested_template
        assert "10.10.0.0/16" in event.mermaid_source
        assert "192.168.99.0/24" not in event.mermaid_source

    @pytest.mark.asyncio
    async def test_show_architecture_diagram_emits_candidate_index(self, tmp_path):
        template = tmp_path / "template.yml"
        template.write_text("ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n", encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "template.yml", "candidate_name": "Same", "candidate_index": 1},
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.candidate_name == "Same"
        assert event.candidate_index == 1

    @pytest.mark.asyncio
    async def test_facts_mode_emits_draft_then_optimized_diagram(self, tmp_path, monkeypatch):
        template = tmp_path / "template.yml"
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        async def fake_create_semantic_plan_for_architecture_with_llm(
            architecture_context: dict,
            template_content: str,
            **_: object,
        ):
            assert architecture_context
            assert "SLB" in template_content
            return {
                "edges": [
                    {
                        "from": "SLB",
                        "to": "ECS1",
                        "kind": "traffic",
                        "label": "后端转发",
                        "confidence": "high",
                    }
                ]
            }

        monkeypatch.setattr(
            show_diagram_tool,
            "create_semantic_plan_for_architecture_with_llm",
            fake_create_semantic_plan_for_architecture_with_llm,
        )

        result = await tool.execute(
            tool_input={
                "file_path": "template.yml",
                "candidate_name": "semantic plan candidate",
                "candidate_index": 0,
                "mode": "facts",
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        draft_event = queue.get_nowait()
        optimized_event = queue.get_nowait()
        assert isinstance(draft_event, DiagramEvent)
        assert isinstance(optimized_event, DiagramEvent)
        assert draft_event.diagram_stage == "draft"
        assert optimized_event.diagram_stage == "optimized"
        assert len(draft_event.views) == 1
        assert draft_event.views[0]["id"] == "overview"
        assert "graph TD" in draft_event.views[0]["mermaid_source"]
        assert "后端转发" in optimized_event.mermaid_source
        assert "Architecture semantic planning instructions" not in result.content
        assert '"visible_nodes"' not in result.content

    @pytest.mark.asyncio
    async def test_facts_mode_uses_internal_preview_semantic_planning_for_large_templates(self, tmp_path, monkeypatch):
        template = tmp_path / "template.yml"
        template.write_text(_many_resource_template(), encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()
        captured_contexts: list[dict] = []

        async def fake_create_semantic_plan_for_architecture_with_llm(
            architecture_context: dict,
            template_content: str,
            **_: object,
        ):
            captured_contexts.append(architecture_context)
            assert "Resource0" in template_content
            return {}

        monkeypatch.setattr(
            show_diagram_tool,
            "create_semantic_plan_for_architecture_with_llm",
            fake_create_semantic_plan_for_architecture_with_llm,
        )

        result = await tool.execute(
            tool_input={
                "file_path": "template.yml",
                "candidate_name": "large candidate",
                "candidate_index": 0,
                "mode": "facts",
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        assert captured_contexts
        draft_event = queue.get_nowait()
        optimized_event = queue.get_nowait()
        assert isinstance(draft_event, DiagramEvent)
        assert isinstance(optimized_event, DiagramEvent)
        assert draft_event.diagram_stage == "draft"
        assert optimized_event.diagram_stage == "optimized"
        assert len(result.content) < 1000
        assert "MUST call show_architecture_diagram again with mode=render" not in result.content
        assert "Architecture semantic planning instructions" not in result.content
        assert '"visible_nodes"' not in result.content
        assert '"resources"' not in result.content

    @pytest.mark.asyncio
    async def test_facts_mode_emits_optimized_diagram_from_internal_llm_plan(self, tmp_path, monkeypatch):
        template = tmp_path / "templates" / "public-network-architecture-design.yml"
        template.parent.mkdir()
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()
        captured_contexts: list[dict] = []

        async def fake_create_semantic_plan_for_architecture_with_llm(
            architecture_context: dict,
            template_content: str,
            **_: object,
        ):
            captured_contexts.append(architecture_context)
            assert "SLB" in template_content
            return {
                "edges": [
                    {
                        "from": "SLB",
                        "to": "ECS1",
                        "kind": "traffic",
                        "label": "后端转发",
                        "confidence": "high",
                    }
                ],
                "views": [
                    {
                        "id": "detail_app",
                        "title": "应用负载详情",
                        "purpose": "展示负载均衡到后端服务器的流量路径",
                        "layout": "flat",
                        "anchors": ["SLB"],
                        "groups": [],
                        "nodes": ["SLB", "ECS1"],
                        "edges": [
                            {
                                "from": "SLB",
                                "to": "ECS1",
                                "kind": "traffic",
                                "label": "后端转发",
                            }
                        ],
                    }
                ],
            }

        monkeypatch.setattr(
            show_diagram_tool,
            "create_semantic_plan_for_architecture_with_llm",
            fake_create_semantic_plan_for_architecture_with_llm,
            raising=False,
        )

        result = await tool.execute(
            tool_input={
                "file_path": "templates/public-network-architecture-design.yml",
                "candidate_name": "semantic plan candidate",
                "candidate_index": 0,
                "mode": "facts",
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        draft_event = queue.get_nowait()
        optimized_event = queue.get_nowait()
        assert isinstance(draft_event, DiagramEvent)
        assert isinstance(optimized_event, DiagramEvent)
        assert draft_event.diagram_stage == "draft"
        assert optimized_event.diagram_stage == "optimized"
        assert optimized_event.views[0]["id"] == "detail_app"
        assert "后端转发" in optimized_event.views[0]["mermaid_source"]
        assert captured_contexts
        assert "Architecture semantic planning instructions" not in result.content
        assert '"visible_nodes"' not in result.content

    @pytest.mark.asyncio
    async def test_facts_mode_emits_fallback_optimized_event_when_executor_cancels_llm(self, tmp_path, monkeypatch):
        class ShortTimeoutShowArchitectureDiagramTool(ShowArchitectureDiagramTool):
            @property
            def timeout(self) -> float | None:
                return 0.05

        template = tmp_path / "template.yml"
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        registry = ToolRegistry()
        registry.register(ShortTimeoutShowArchitectureDiagramTool())
        executor = ToolExecutor(registry, tool_timeout=0.05)

        async def slow_create_semantic_plan_for_architecture_with_llm(
            architecture_context: dict,
            template_content: str,
            **_: object,
        ):
            assert architecture_context
            assert template_content
            await asyncio.sleep(10)
            return {}

        monkeypatch.setattr(
            show_diagram_tool,
            "create_semantic_plan_for_architecture_with_llm",
            slow_create_semantic_plan_for_architecture_with_llm,
        )

        results = await executor.execute_batch(
            [
                ToolCallRequest(
                    id="call_1",
                    name="show_architecture_diagram",
                    input={
                        "file_path": "template.yml",
                        "candidate_name": "semantic plan candidate",
                        "candidate_index": 0,
                        "mode": "facts",
                    },
                    event_queue=queue,
                )
            ],
            ToolContext(cwd=str(tmp_path)),
        )

        assert results[0].is_error
        draft_event = queue.get_nowait()
        optimized_event = queue.get_nowait()
        assert isinstance(draft_event, DiagramEvent)
        assert isinstance(optimized_event, DiagramEvent)
        assert draft_event.diagram_stage == "draft"
        assert optimized_event.diagram_stage == "optimized"

    @pytest.mark.asyncio
    async def test_render_mode_applies_valid_semantic_plan(self, tmp_path):
        template = tmp_path / "template.yml"
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={
                "file_path": "template.yml",
                "candidate_name": "semantic plan candidate",
                "candidate_index": 0,
                "semantic_plan": {
                    "edges": [
                        {
                            "from": "SLB",
                            "to": "ECS1",
                            "kind": "traffic",
                            "label": "forwards traffic",
                            "confidence": "medium",
                        }
                    ]
                },
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.diagram_stage == "optimized"
        assert "SLB -->|forwards traffic| ECS1" in event.mermaid_source
        assert "SLB --> ECS1" not in event.mermaid_source
        assert event.architecture_context is not None
        assert event.architecture_context["semantic_plan"]["accepted_edges"][0]["label"] == "forwards traffic"

    @pytest.mark.asyncio
    async def test_render_mode_emits_multiple_views_from_semantic_plan(self, tmp_path):
        template = tmp_path / "template.yml"
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={
                "file_path": "template.yml",
                "candidate_name": "multi view candidate",
                "candidate_index": 0,
                "mode": "render",
                "semantic_plan": {
                    "views": [
                        {
                            "id": "overview",
                            "title": "总览",
                            "purpose": "整体拓扑",
                            "node_ids": ["SLB", "ECS1"],
                            "edge_ids": [],
                        },
                        {
                            "id": "detail_app",
                            "title": "应用详情",
                            "purpose": "应用层",
                            "node_ids": ["SLB", "ECS1", "ECS2"],
                            "edge_ids": [],
                        },
                    ]
                },
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.diagram_stage == "optimized"
        assert [(view["id"], view["title"]) for view in event.views] == [
            ("overview", "总览"),
            ("detail_app", "应用详情"),
        ]

    @pytest.mark.asyncio
    async def test_render_mode_repairs_preview_semantic_plan_before_rendering(self, tmp_path):
        template = tmp_path / "template.yml"
        template.write_text(SLB_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={
                "file_path": "template.yml",
                "candidate_name": "repaired plan candidate",
                "candidate_index": 0,
                "mode": "render",
                "semantic_plan": {
                    "node_labels": [
                        {"id": "SLB", "label": "入口负载均衡", "confidence": "high", "reason": "obvious"},
                        {"id": "Missing", "label": "不存在", "confidence": "high", "reason": "bad"},
                    ],
                    "edges": [
                        {"from": "SLB", "to": "ECS1", "kind": "traffic", "label": "后端转发"},
                        {"from": "SLB", "to": "Missing", "kind": "traffic", "label": "错误关系"},
                    ],
                    "views": [
                        {
                            "id": "detail_app",
                            "layout": "deep",
                            "nodes": ["SLB", "Missing"],
                            "edges": [{"from": "SLB", "to": "Missing", "kind": "traffic", "label": "错误关系"}],
                        },
                        {
                            "id": "overview",
                            "layout": "flat",
                            "nodes": ["SLB", "ECS1"],
                            "edges": [{"from": "SLB", "to": "ECS1", "kind": "traffic", "label": "后端转发"}],
                        },
                    ],
                },
            },
            context=ToolContext(cwd=str(tmp_path), event_queue=queue),
        )

        assert not result.is_error
        event = queue.get_nowait()
        assert isinstance(event, DiagramEvent)
        assert event.diagram_stage == "optimized"
        assert [(view["id"], view["title"]) for view in event.views] == [
            ("overview", "overview"),
            ("detail_app", "detail_app"),
        ]
        accepted_labels = event.architecture_context["semantic_plan"]["accepted_node_labels"]
        assert accepted_labels == [{"id": "SLB", "label": "入口负载均衡", "confidence": "high"}]
        assert all("Missing" not in view["mermaid_source"] for view in event.views)

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path):
        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(cwd=str(tmp_path), event_queue=queue)
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "templates/nonexistent.yml", "candidate_name": "不存在", "candidate_index": 0},
            context=context,
        )

        assert result.is_error
        _assert_error_diagram_event(queue, candidate_name="不存在", expected_text="Template file does not exist")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_path", ["../secret.yml", "../../secret.yml"])
    async def test_rejects_parent_directory_escape(self, tmp_path, file_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "secret.yml").write_text(SIMPLE_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": file_path, "candidate_name": "逃逸方案", "candidate_index": 0},
            context=ToolContext(cwd=str(workspace), event_queue=queue),
        )

        assert result.is_error
        _assert_error_diagram_event(queue, candidate_name="逃逸方案", expected_text="cannot escape")

    @pytest.mark.asyncio
    async def test_rejects_absolute_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "secret.yml"
        outside.write_text(SIMPLE_TEMPLATE, encoding="utf-8")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": str(outside), "candidate_name": "绝对路径方案", "candidate_index": 0},
            context=ToolContext(cwd=str(workspace), event_queue=queue),
        )

        assert result.is_error
        _assert_error_diagram_event(queue, candidate_name="绝对路径方案", expected_text="must be relative")

    @pytest.mark.asyncio
    async def test_rejects_symlink_escape(self, tmp_path):
        workspace = tmp_path / "workspace"
        templates = workspace / "templates"
        templates.mkdir(parents=True)
        outside = tmp_path / "secret.yml"
        outside.write_text(SIMPLE_TEMPLATE, encoding="utf-8")
        link = templates / "linked.yml"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable on this platform")
        queue: asyncio.Queue = asyncio.Queue()
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "templates/linked.yml", "candidate_name": "链接逃逸方案", "candidate_index": 0},
            context=ToolContext(cwd=str(workspace), event_queue=queue),
        )

        assert result.is_error
        _assert_error_diagram_event(queue, candidate_name="链接逃逸方案", expected_text="cannot escape")

    @pytest.mark.asyncio
    async def test_no_event_queue(self, tmp_path):
        """Tool works even without an event queue (no diagram emitted, just returns summary)."""
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        (template_dir / "1-simple.yml").write_text(SIMPLE_TEMPLATE, encoding="utf-8")

        context = ToolContext(cwd=str(tmp_path), event_queue=None)
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "templates/1-simple.yml", "candidate_name": "简单方案"},
            context=context,
        )
        assert not result.is_error


class TestRosTagParsing:
    """Regression: mermaid generation must accept ROS intrinsic-function tags."""

    def test_template_with_ref_tag_renders(self):
        from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid

        # A minimal ROS template that uses !Ref — yaml.safe_load would
        # reject this; ros_yaml_load handles it.
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VpcId: !Ref VPC
      InstanceType: ecs.g6.large
"""
        result = ros_template_to_mermaid(template)
        # No fallback error path — actual graph rendered
        assert "Error[YAML parse error]" not in result
        # Some recognizable mermaid content from the resources
        assert "graph TD" in result

    def test_template_with_getatt_tag_renders(self):
        from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid

        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      InstanceType: ecs.g6.large
Outputs:
  PublicIp:
    Value: !GetAtt ECS.PublicIp
"""
        result = ros_template_to_mermaid(template)
        assert "Error[YAML parse error]" not in result
        assert "graph TD" in result

    def test_invalid_yaml_still_falls_back(self):
        """Genuinely malformed YAML still hits the fallback (sanity)."""
        from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid

        template = "this is: not\n  - valid: yaml: at: all"
        result = ros_template_to_mermaid(template)
        # Either "Error[YAML parse error]" fallback or `"graph TD"` (empty) is acceptable
        # as long as it doesn't raise.
        assert "graph TD" in result


class TestShowArchitectureDiagramToolDoesNotBlockEventLoop:
    """Rendering walks the whole template graph (pure CPU); it must run off the
    event loop (asyncio.to_thread) so it never starves web turns / SSE / handlers.
    """

    @pytest.mark.asyncio
    async def test_render_does_not_starve_loop(self, tmp_path, monkeypatch):
        import threading

        (tmp_path / "template.yml").write_text(SIMPLE_TEMPLATE, encoding="utf-8")

        entered = threading.Event()
        release = threading.Event()
        sentinel = show_diagram_tool.render_ros_template_architecture_views(SIMPLE_TEMPLATE)

        def blocking_render(*args, **kwargs):
            entered.set()
            release.wait(5)
            return sentinel

        monkeypatch.setattr(show_diagram_tool, "render_ros_template_architecture_views", blocking_render)

        queue: asyncio.Queue = asyncio.Queue()
        context = ToolContext(cwd=str(tmp_path), event_queue=queue)
        tool = ShowArchitectureDiagramTool()
        task = asyncio.create_task(
            tool.execute(
                tool_input={"file_path": "template.yml", "candidate_name": "方案"},
                context=context,
            )
        )

        # Worker thread entered the blocking render while the loop stayed free.
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
        assert not task.done()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()

        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert not result.is_error
        assert not queue.empty()
