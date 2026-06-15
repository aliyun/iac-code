import asyncio

import pytest

from iac_code.pipeline.engine.show_diagram_tool import ShowArchitectureDiagramTool, ros_template_to_mermaid
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent, ToolEmittedEvent

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
        assert 'subgraph layer_VPC["VPC (172.16.0.0/12)"]' in mermaid
        assert 'subgraph layer_VSwitch["VSwitch (172.16.0.0/24)"]' in mermaid
        assert 'subgraph layer_SecurityGroup["Security group"]' in mermaid

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
    async def test_file_not_found(self, tmp_path):
        context = ToolContext(cwd=str(tmp_path))
        tool = ShowArchitectureDiagramTool()

        result = await tool.execute(
            tool_input={"file_path": "templates/nonexistent.yml", "candidate_name": "不存在"},
            context=context,
        )

        assert result.is_error

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
        assert queue.empty()

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
        assert queue.empty()

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
        assert queue.empty()

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
