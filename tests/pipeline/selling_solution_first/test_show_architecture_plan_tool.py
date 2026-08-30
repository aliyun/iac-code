"""Progressive Step 1 outline and rich-detail display tools."""

from __future__ import annotations

import asyncio

import pytest

from iac_code.pipeline.selling_solution_first.tools.show_architecture_plan_tool import ShowArchitecturePlanTool
from iac_code.pipeline.selling_solution_first.tools.show_candidate_detail_tool import ShowCandidateDetailTool
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import CandidateDetailEvent, DiagramEvent

NODES = [
    {"id": "slb", "label": "公网 SLB", "product": "SLB", "role": "入口", "group": "vpc"},
    {"id": "ecs", "label": "Web ECS x 2", "product": "ECS", "role": "应用计算", "group": "vpc"},
    {"id": "rds", "label": "RDS MySQL", "product": "RDS", "role": "数据库", "group": "vpc"},
    {"id": "oss", "label": "OSS 静态资源", "product": "OSS", "role": "对象存储"},
]
EDGES = [
    {"source": "slb", "target": "ecs", "label": "HTTPS"},
    {"source": "ecs", "target": "rds", "relation": "depends_on"},
    {"source": "ecs", "target": "oss"},
]


def _tool_input(**overrides):
    nodes = overrides.pop("nodes", NODES)
    edges = overrides.pop("edges", EDGES)
    payload = {
        "candidate_name": "方案A：经典三层",
        "candidate_index": 0,
        "applicable_scenarios": ["生产站点"],
        "resource_intents": [{"product": "ECS", "action": "create"}],
        "topology_graph": {"nodes": nodes, "edges": edges},
        "resource_inventory": [
            {
                "resource_id": "ecs",
                "product": "ECS",
                "purpose": "应用计算",
                "quantity": 2,
                "recommended_spec": "2 vCPU / 4 GiB",
                "rough_monthly_cost": "¥200～¥400/月",
                "lifecycle": "create",
            }
        ],
        "cost_assumptions": ["cn-hangzhou"],
        "cost_exclusions": ["公网流量"],
        "cost_confidence": "medium",
        "decision_notes": {
            "why_recommended": ["符合站点需求"],
            "problems_solved": ["提供应用计算"],
            "pros": ["架构清晰", "便于扩展"],
            "cons": ["有固定费用"],
        },
    }
    payload.update(overrides)
    return payload


async def _run(tool_input) -> tuple[object, list]:
    queue: asyncio.Queue = asyncio.Queue()
    state = {
        "tool_result_records": [
            {
                "tool_name": "show_architecture_plan",
                "input": {
                    "candidates": [
                        {
                            "candidate_name": "方案A：经典三层",
                            "summary": "经典三层架构",
                            "total_monthly_cost": "¥200～¥400/月",
                            "key_tradeoff": "组件较多",
                        }
                    ]
                },
                "result": {},
                "is_error": False,
                "record_id": "tu_plan",
                "sequence": 1,
            }
        ]
    }
    result = await ShowCandidateDetailTool(state).execute(
        tool_input=tool_input, context=ToolContext(event_queue=queue, tool_use_id="tu_detail")
    )
    events = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, DiagramEvent):
            events.append(event)
    return result, events


class TestMetadata:
    def test_tool_is_read_only_and_needs_the_event_queue(self):
        outline = ShowArchitecturePlanTool()
        detail = ShowCandidateDetailTool()

        assert outline.name == "show_architecture_plan"
        assert outline.is_read_only({}) is True
        assert outline.needs_event_queue() is True
        assert detail.name == "show_candidate_detail"
        assert detail.is_read_only({}) is True
        assert detail.needs_event_queue() is True

    def test_outline_schema_is_small_and_detail_owns_the_graph(self):
        outline_schema = ShowArchitecturePlanTool().input_schema
        detail_schema = ShowCandidateDetailTool().input_schema

        assert outline_schema["required"] == ["candidates"]
        outline = outline_schema["properties"]["candidates"]["items"]
        assert outline["required"] == ["candidate_name", "summary", "total_monthly_cost", "key_tradeoff"]
        assert "nodes" not in outline["properties"]
        graph = detail_schema["properties"]["topology_graph"]
        assert graph["required"] == ["nodes", "edges"]
        assert "summary" not in detail_schema["properties"]

    @pytest.mark.asyncio
    async def test_outline_call_emits_one_refining_card_per_candidate(self):
        queue: asyncio.Queue = asyncio.Queue()
        state = {"tool_result_records": []}
        result = await ShowArchitecturePlanTool(state).execute(
            tool_input={
                "candidates": [
                    {
                        "candidate_name": "轻量方案",
                        "summary": "单机",
                        "total_monthly_cost": "¥100～¥200/月",
                        "key_tradeoff": "单点",
                    },
                    {
                        "candidate_name": "高可用方案",
                        "summary": "双机",
                        "total_monthly_cost": "¥300～¥500/月",
                        "key_tradeoff": "成本较高",
                    },
                ]
            },
            context=ToolContext(event_queue=queue, tool_use_id="tu_outline"),
        )

        events = [queue.get_nowait(), queue.get_nowait()]
        assert result.is_error is False
        assert all(isinstance(event, CandidateDetailEvent) for event in events)
        assert [event.candidate_index for event in events] == [0, 1]
        assert [event.candidate_set_id for event in events] == ["tu_outline", "tu_outline"]
        assert [event.detail_stage for event in events] == ["outline", "outline"]
        assert [event.key_tradeoff for event in events] == ["单点", "成本较高"]
        assert result.metadata == {"candidate_set_id": "tu_outline"}

    @pytest.mark.asyncio
    async def test_identical_outline_batch_is_idempotent_and_emits_no_duplicate_cards(self):
        candidates = [
            {
                "candidate_name": "轻量方案",
                "summary": "单机",
                "total_monthly_cost": "¥100～¥200/月",
                "key_tradeoff": "单点",
            }
        ]
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {"candidates": candidates},
                    "result": {},
                    "is_error": False,
                    "record_id": "outline-first",
                    "candidate_set_id": "outline-first",
                    "sequence": 1,
                }
            ]
        }
        queue: asyncio.Queue = asyncio.Queue()

        result = await ShowArchitecturePlanTool(state).execute(
            tool_input={"candidates": candidates},
            context=ToolContext(event_queue=queue, tool_use_id="outline-duplicate"),
        )

        assert result.is_error is False
        assert result.metadata == {"candidate_set_id": "outline-first", "idempotent": True}
        assert "Do not repeat show_architecture_plan" in result.content
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_changed_outline_batch_still_starts_a_new_candidate_set(self):
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "原方案",
                                "summary": "单机",
                                "total_monthly_cost": "¥100/月",
                                "key_tradeoff": "单点",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "outline-old",
                    "candidate_set_id": "outline-old",
                    "sequence": 1,
                }
            ]
        }
        queue: asyncio.Queue = asyncio.Queue()
        candidates = [
            {
                "candidate_name": "原方案",
                "summary": "单机",
                "total_monthly_cost": "¥100/月",
                "key_tradeoff": "单点",
            },
            {
                "candidate_name": "新增方案",
                "summary": "轻量服务器",
                "total_monthly_cost": "¥200/月",
                "key_tradeoff": "规格受限",
            },
        ]

        result = await ShowArchitecturePlanTool(state).execute(
            tool_input={"candidates": candidates},
            context=ToolContext(event_queue=queue, tool_use_id="outline-new"),
        )

        assert result.is_error is False
        assert result.metadata == {"candidate_set_id": "outline-new"}
        assert queue.qsize() == 2

    @pytest.mark.parametrize("count", [1, 3])
    @pytest.mark.asyncio
    async def test_outline_accepts_the_supported_batch_sizes(self, count):
        queue: asyncio.Queue = asyncio.Queue()
        candidates = [
            {
                "candidate_name": f"方案 {index}",
                "summary": f"摘要 {index}",
                "total_monthly_cost": f"¥{index + 1}00/月",
                "key_tradeoff": f"取舍 {index}",
            }
            for index in range(count)
        ]

        result = await ShowArchitecturePlanTool().execute(
            tool_input={"candidates": candidates},
            context=ToolContext(event_queue=queue, tool_use_id="batch"),
        )

        assert result.is_error is False
        assert queue.qsize() == count

    @pytest.mark.parametrize(
        "candidates",
        [
            [],
            [
                {
                    "candidate_name": "重复",
                    "summary": "a",
                    "total_monthly_cost": "¥1/月",
                    "key_tradeoff": "x",
                },
                {
                    "candidate_name": "重复",
                    "summary": "b",
                    "total_monthly_cost": "¥2/月",
                    "key_tradeoff": "y",
                },
            ],
            [
                {
                    "candidate_name": "方案",
                    "summary": " ",
                    "total_monthly_cost": "¥1/月",
                    "key_tradeoff": "x",
                }
            ],
            [
                {
                    "candidate_name": f"方案 {index}",
                    "summary": "摘要",
                    "total_monthly_cost": "¥1/月",
                    "key_tradeoff": "取舍",
                }
                for index in range(4)
            ],
        ],
    )
    @pytest.mark.asyncio
    async def test_outline_rejects_invalid_or_oversized_batches(self, candidates):
        result = await ShowArchitecturePlanTool().execute(
            tool_input={"candidates": candidates},
            context=ToolContext(tool_use_id="batch"),
        )

        assert result.is_error is True
        assert "candidates must be" in result.content


class TestDetailOrderingGate:
    @pytest.mark.asyncio
    async def test_detail_is_rejected_without_a_successful_outline(self):
        result = await ShowCandidateDetailTool({"tool_result_records": []}).execute(
            tool_input=_tool_input(),
            context=ToolContext(),
        )

        assert result.is_error is True
        assert "before a successful show_architecture_plan" in result.content

    @pytest.mark.asyncio
    async def test_detail_must_follow_current_batch_index_and_name(self):
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "方案 A",
                                "summary": "A",
                                "total_monthly_cost": "¥1/月",
                                "key_tradeoff": "A 取舍",
                            },
                            {
                                "candidate_name": "方案 B",
                                "summary": "B",
                                "total_monthly_cost": "¥2/月",
                                "key_tradeoff": "B 取舍",
                            },
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "batch-2",
                    "sequence": 1,
                }
            ]
        }

        result = await ShowCandidateDetailTool(state).execute(
            tool_input=_tool_input(candidate_index=1, candidate_name="方案 B"),
            context=ToolContext(),
        )

        assert result.is_error is True
        assert "expected candidate_index=0" in result.content
        assert "candidate_name='方案 A'" in result.content

    @pytest.mark.asyncio
    async def test_new_outline_batch_invalidates_old_successful_details(self):
        old_detail = _tool_input(candidate_name="旧方案")
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "旧方案",
                                "summary": "旧",
                                "total_monthly_cost": "¥1/月",
                                "key_tradeoff": "旧取舍",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "old-batch",
                    "sequence": 1,
                },
                {
                    "tool_name": "show_candidate_detail",
                    "input": old_detail,
                    "result": {},
                    "is_error": False,
                    "record_id": "old-detail",
                    "sequence": 2,
                },
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "新方案",
                                "summary": "新",
                                "total_monthly_cost": "¥2/月",
                                "key_tradeoff": "新取舍",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "new-batch",
                    "sequence": 3,
                },
            ]
        }

        result = await ShowCandidateDetailTool(state).execute(
            tool_input=_tool_input(candidate_name="新方案"),
            context=ToolContext(tool_use_id="new-detail"),
        )

        assert result.is_error is False
        assert "candidateSetId=new-batch" in result.content

    @pytest.mark.asyncio
    async def test_restored_partial_batch_continues_with_only_the_first_missing_candidate(self):
        detail_zero = _tool_input(candidate_index=0, candidate_name="方案 A")
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "方案 A",
                                "summary": "A",
                                "total_monthly_cost": "¥1/月",
                                "key_tradeoff": "A 取舍",
                            },
                            {
                                "candidate_name": "方案 B",
                                "summary": "B",
                                "total_monthly_cost": "¥2/月",
                                "key_tradeoff": "B 取舍",
                            },
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "restored-batch",
                    "sequence": 1,
                },
                {
                    "tool_name": "show_candidate_detail",
                    "input": detail_zero,
                    "result": {},
                    "is_error": False,
                    "record_id": "detail-0",
                    "sequence": 2,
                },
            ]
        }

        result = await ShowCandidateDetailTool(state).execute(
            tool_input=_tool_input(candidate_index=1, candidate_name="方案 B"),
            context=ToolContext(tool_use_id="detail-1"),
        )

        assert result.is_error is False
        assert "candidate 1" in result.content
        assert "candidateSetId=restored-batch" in result.content
        assert result.metadata == {"candidate_set_id": "restored-batch"}

    @pytest.mark.asyncio
    async def test_detail_from_an_explicit_old_batch_does_not_satisfy_the_new_batch(self):
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "同名方案",
                                "summary": "旧摘要",
                                "total_monthly_cost": "¥1/月",
                                "key_tradeoff": "旧取舍",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "old-batch",
                    "sequence": 1,
                },
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "同名方案",
                                "summary": "新摘要",
                                "total_monthly_cost": "¥2/月",
                                "key_tradeoff": "新取舍",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "new-batch",
                    "sequence": 2,
                },
                {
                    "tool_name": "show_candidate_detail",
                    "input": _tool_input(candidate_name="同名方案"),
                    "result": {},
                    "is_error": False,
                    "record_id": "concurrent-old-detail",
                    "sequence": 3,
                    "candidate_set_id": "old-batch",
                },
            ]
        }

        result = await ShowCandidateDetailTool(state).execute(
            tool_input=_tool_input(candidate_name="同名方案"),
            context=ToolContext(tool_use_id="new-detail"),
        )

        assert result.is_error is False
        assert "candidateSetId=new-batch" in result.content
        assert result.metadata == {"candidate_set_id": "new-batch"}


class TestSuccessfulRender:
    @pytest.mark.asyncio
    async def test_valid_graph_renders_parseable_mermaid_flowchart(self):
        result, events = await _run(_tool_input())

        assert result.is_error is False
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, DiagramEvent)
        assert event.candidate_name == "方案A：经典三层"
        assert event.candidate_index == 0
        assert event.template_content == ""
        assert event.diagram_stage == "optimized"
        assert [view["id"] for view in event.views] == ["overview"]
        assert event.views[0]["mermaid_source"] == event.mermaid_source

        lines = event.mermaid_source.splitlines()
        assert lines[0] == "flowchart TD"
        assert any(line.strip().startswith("subgraph group_vpc[") for line in lines)
        assert lines.count("  end") == 1
        # label 已经写了产品名时，第二行只留 role，不再重复一遍产品。
        assert '    ecs["Web ECS x 2\\n应用计算"]' in lines
        assert '  oss["OSS 静态资源\\n对象存储"]' in lines
        assert "  slb -->|HTTPS| ecs" in lines
        assert "  ecs -->|depends_on| rds" in lines
        assert "  ecs --> oss" in lines
        # 括号/引号等 Mermaid 语法字符不得出现在标签之外的位置。
        assert event.mermaid_source.count('"') % 2 == 0

    @pytest.mark.asyncio
    async def test_architecture_context_carries_plan_ids_for_downstream_surfaces(self):
        _result, events = await _run(_tool_input())

        context = events[0].architecture_context
        assert context["source"] == "architecture_plan"
        assert [node["plan_id"] for node in context["nodes"]] == ["slb", "ecs", "rds", "oss"]
        assert [group["plan_id"] for group in context["groups"]] == ["vpc"]
        assert len(context["edges"]) == 3
        assert context["warnings"] == []

    @pytest.mark.asyncio
    async def test_special_characters_do_not_break_mermaid_syntax(self):
        nodes = [
            {"id": "web-01 (主)", "label": 'Web "主" 节点 [primary]', "product": "ECS|计算"},
            {"id": "db;drop", "label": "RDS <主库>", "product": "RDS"},
        ]
        result, events = await _run(
            _tool_input(nodes=nodes, edges=[{"source": "web-01 (主)", "target": "db;drop", "label": 'a"b|c'}])
        )

        assert result.is_error is False
        source = events[0].mermaid_source
        for hostile in ('"主"', "[primary]", "|计算", "<主库>", ";drop"):
            assert hostile not in source
        node_lines = [line.strip() for line in source.splitlines() if line.strip().endswith('"]')]
        assert len(node_lines) == 2
        for line in node_lines:
            identifier = line.split("[", 1)[0]
            assert identifier.replace("_", "").isalnum()
        edge_line = [line for line in source.splitlines() if "-->" in line][0]
        assert edge_line.count("|") == 2

    @pytest.mark.asyncio
    async def test_long_labels_are_capped(self):
        result, events = await _run(_tool_input(nodes=[{"id": "n1", "label": "字" * 200, "product": "ECS"}], edges=[]))

        assert result.is_error is False
        label = events[0].architecture_context["nodes"][0]["label"]
        assert len(label) <= 60
        assert label.endswith("…")

    @pytest.mark.asyncio
    async def test_detail_line_keeps_product_when_the_label_does_not_say_it(self):
        result, events = await _run(
            _tool_input(
                nodes=[{"id": "user", "label": "公网用户", "product": "Internet", "role": "访问入口"}],
                edges=[],
            )
        )

        assert result.is_error is False
        assert '  user["公网用户\\nInternet · 访问入口"]' in events[0].mermaid_source.splitlines()

    @pytest.mark.asyncio
    async def test_detail_line_drops_a_role_that_only_repeats_the_product(self):
        result, events = await _run(
            _tool_input(nodes=[{"id": "cdn", "label": "内容分发", "product": "CDN", "role": "cdn"}], edges=[])
        )

        assert result.is_error is False
        assert '  cdn["内容分发\\nCDN"]' in events[0].mermaid_source.splitlines()

    @pytest.mark.asyncio
    async def test_each_rendered_line_is_capped_tighter_than_the_stored_label(self):
        result, events = await _run(
            _tool_input(
                nodes=[{"id": "n1", "label": "长" * 200, "product": "ECS", "role": "角" * 200}],
                edges=[],
            )
        )

        assert result.is_error is False
        node_line = [line for line in events[0].mermaid_source.splitlines() if line.strip().startswith("n1[")][0]
        primary, detail = node_line.split('["', 1)[1].removesuffix('"]').split("\\n")
        # 存下来的 label 仍可到 60 字，但渲染出的每一行必须更短，否则方框会撑得没法看。
        assert len(primary) <= 28 and primary.endswith("…")
        assert len(detail) <= 20 and detail.endswith("…")
        assert len(events[0].architecture_context["nodes"][0]["label"]) > 28


class TestValidationFailures:
    @pytest.mark.asyncio
    async def test_duplicate_node_id_is_rejected(self):
        result, events = await _run(
            _tool_input(
                nodes=[
                    {"id": "ecs", "label": "A", "product": "ECS"},
                    {"id": "ecs", "label": "B", "product": "ECS"},
                ],
                edges=[],
            )
        )

        assert result.is_error is True
        assert "Duplicate node id: ecs" in result.content
        assert events == []

    @pytest.mark.asyncio
    async def test_empty_or_non_list_nodes_are_rejected(self):
        for bad in ([], None, {"id": "ecs"}):
            result, _events = await _run(_tool_input(nodes=bad))
            assert result.is_error is True
            assert "nodes must be a non-empty array" in result.content

    @pytest.mark.asyncio
    async def test_node_without_id_is_rejected(self):
        result, _events = await _run(_tool_input(nodes=[{"id": "  ", "label": "A", "product": "ECS"}], edges=[]))

        assert result.is_error is True
        assert "nodes[0].id must not be empty" in result.content

    @pytest.mark.asyncio
    async def test_empty_candidate_name_is_rejected(self):
        result, _events = await _run(_tool_input(candidate_name="   "))

        assert result.is_error is True
        assert "expected candidate_index=0" in result.content

    @pytest.mark.parametrize("bad_index", [-1, "0", 1.5, True, None])
    @pytest.mark.asyncio
    async def test_out_of_range_or_non_integer_candidate_index_is_rejected(self, bad_index):
        result, events = await _run(_tool_input(candidate_index=bad_index))

        assert result.is_error is True
        assert "expected candidate_index=0" in result.content
        assert events == []

    @pytest.mark.asyncio
    async def test_graph_failure_is_a_hard_detail_error(self):
        result, _events = await _run(_tool_input(nodes=[]))

        assert result.is_error is True
        assert "Failed to render the candidate topology" in result.content


class TestGroupContainerFolding:
    """模型常同时给出 ``vpc`` 节点和 ``group: "vpc"``，渲染时要折成一个子图而不是画三遍。"""

    CONTAINER_NODES = [
        {"id": "vpc", "label": "VPC", "product": "VPC", "role": "虚拟私有网络"},
        {"id": "vsw", "label": "VSwitch 可用区A", "product": "VSwitch", "role": "交换机", "group": "vpc"},
        {"id": "user", "label": "公网用户", "product": "Internet", "role": "访问入口"},
    ]
    CONTAINER_EDGES = [
        {"source": "vpc", "target": "vsw", "label": "包含"},
        {"source": "vsw", "target": "vpc", "label": "归属"},
        {"source": "user", "target": "vpc", "label": "访问"},
    ]

    @pytest.mark.asyncio
    async def test_group_node_becomes_the_subgraph_title_without_its_own_box(self):
        result, events = await _run(_tool_input(nodes=self.CONTAINER_NODES, edges=self.CONTAINER_EDGES))

        assert result.is_error is False
        lines = events[0].mermaid_source.splitlines()
        # 子图标题用节点的展示 label，而不是原始的小写 group 字符串。
        assert '  subgraph group_vpc["VPC 虚拟私有网络"]' in lines
        assert not any(line.strip().startswith("vpc[") for line in lines)
        assert '    vsw["VSwitch 可用区A\\n交换机"]' in lines

    @pytest.mark.asyncio
    async def test_containment_edges_are_dropped_and_outside_edges_point_at_the_subgraph(self):
        _result, events = await _run(_tool_input(nodes=self.CONTAINER_NODES, edges=self.CONTAINER_EDGES))

        edge_lines = [line for line in events[0].mermaid_source.splitlines() if "-->" in line]
        assert edge_lines == ["  user -->|访问| group_vpc"]

    @pytest.mark.asyncio
    async def test_architecture_context_still_carries_the_folded_node_and_edges(self):
        _result, events = await _run(_tool_input(nodes=self.CONTAINER_NODES, edges=self.CONTAINER_EDGES))

        # 折叠只发生在渲染层：下游拿到的结构化图仍然是模型给的原图。
        context = events[0].architecture_context
        assert [node["plan_id"] for node in context["nodes"]] == ["vpc", "vsw", "user"]
        assert len(context["edges"]) == 3

    @pytest.mark.asyncio
    async def test_node_that_is_not_used_as_a_group_keeps_its_own_box(self):
        _result, events = await _run(
            _tool_input(
                nodes=[
                    {"id": "vpc", "label": "VPC", "product": "VPC", "role": "虚拟私有网络"},
                    {"id": "oss", "label": "OSS 静态资源", "product": "OSS", "group": "no-such-member-group"},
                ],
                edges=[],
            )
        )

        lines = events[0].mermaid_source.splitlines()
        assert '  vpc["VPC\\n虚拟私有网络"]' in lines

    @pytest.mark.asyncio
    async def test_container_node_inside_another_group_is_not_folded(self):
        _result, events = await _run(
            _tool_input(
                nodes=[
                    {"id": "vpc", "label": "VPC", "product": "VPC", "group": "region"},
                    {"id": "vsw", "label": "交换机", "product": "VSwitch", "group": "vpc"},
                    {"id": "ecs", "label": "应用服务器", "product": "ECS", "group": "region"},
                ],
                edges=[{"source": "vpc", "target": "vsw", "label": "包含"}],
            )
        )

        lines = events[0].mermaid_source.splitlines()
        # 扁平 schema 表达不了嵌套子图，折叠会悄悄丢掉 vpc 属于 region 这件事，所以保持原样。
        assert '    vpc["VPC"]' in lines
        assert '  subgraph group_vpc["vpc"]' in lines
        assert "  vpc -->|包含| vsw" in lines


class TestDegradedEdges:
    @pytest.mark.asyncio
    async def test_dangling_self_and_duplicate_edges_are_skipped_with_warnings(self):
        result, events = await _run(
            _tool_input(
                edges=[
                    {"source": "slb", "target": "ecs", "label": "HTTPS"},
                    {"source": "slb", "target": "ecs", "label": "HTTPS"},
                    {"source": "ecs", "target": "ecs"},
                    {"source": "ecs", "target": "unknown"},
                    "not-an-object",
                ]
            )
        )

        assert result.is_error is False
        source = events[0].mermaid_source
        assert source.count("-->") == 1
        warnings = events[0].architecture_context["warnings"]
        assert any("references a node id that is not defined" in warning for warning in warnings)
        assert any("self-referencing" in warning for warning in warnings)
        assert any("not an object" in warning for warning in warnings)
        for warning in warnings:
            assert warning in result.content

    @pytest.mark.asyncio
    async def test_non_list_edges_are_rejected(self):
        result, _events = await _run(_tool_input(edges={"source": "slb"}))

        assert result.is_error is True
        assert "edges must be an array" in result.content


class TestWithoutEventQueue:
    @pytest.mark.asyncio
    async def test_missing_event_queue_still_succeeds(self):
        state = {
            "tool_result_records": [
                {
                    "tool_name": "show_architecture_plan",
                    "input": {
                        "candidates": [
                            {
                                "candidate_name": "方案A：经典三层",
                                "summary": "经典三层架构",
                                "total_monthly_cost": "¥200～¥400/月",
                                "key_tradeoff": "组件较多",
                            }
                        ]
                    },
                    "result": {},
                    "is_error": False,
                    "record_id": "tu_plan",
                    "sequence": 1,
                }
            ]
        }
        result = await ShowCandidateDetailTool(state).execute(tool_input=_tool_input(), context=ToolContext())

        assert result.is_error is False
        assert "方案A：经典三层" in result.content
