from types import SimpleNamespace

import iac_code.web.diagram_cache as dc
from iac_code.web.diagrams import CandidateTemplate, diagram_items, iter_candidate_templates
from iac_code.web.outputs import outputs_payload, pipeline_candidate_costs

_ROS_A = (
    "ROSTemplateFormatVersion: '2015-09-01'\n"
    "Resources:\n"
    "  MyVpc:\n"
    "    Type: ALIYUN::ECS::VPC\n"
    "    Properties:\n"
    "      CidrBlock: 192.168.0.0/16\n"
)
_ROS_B = (
    "ROSTemplateFormatVersion: '2015-09-01'\n"
    "Resources:\n"
    "  MyEcs:\n"
    "    Type: ALIYUN::ECS::Instance\n"
    "    Properties: {}\n"
)


def _wf_envelope(path, content, index=None, name=None):
    env = {
        "eventType": "tool_result",
        "data": {"toolName": "write_file", "input": {"path": path, "content": content}},
    }
    if index is not None:
        env["candidate"] = {"index": index, "name": name}
    return env


def _detail_envelope(index, cost_items, total):
    return {
        "eventType": "candidate_detail_shown",
        "data": {
            "candidateIndex": index,
            "detail": {"costItems": cost_items, "totalMonthlyCost": total},
        },
    }


def _completed_envelope(index, name, monthly_estimate, resources):
    # cost_estimating(step3)结论:candidate_completed.data.conclusions.cost。
    # resources 为 [{"type", "cost"}](与 ros_estimate_template_cost 产出一致)。
    return {
        "eventType": "candidate_completed",
        "data": {
            "candidateIndex": index,
            "candidateName": name,
            "conclusions": {
                "cost": {
                    "monthly_estimate": monthly_estimate,
                    "currency": "CNY",
                    "resources": resources,
                },
            },
        },
    }


class _Manager:
    def __init__(self, envelopes):
        self._envelopes = envelopes

    def _load_a2a_pipeline_envelopes(self, context_id):
        return list(self._envelopes)


def _session(tmp_path):
    return SimpleNamespace(cwd=str(tmp_path), session_id="s1", context_id="ctx1")


def test_diagram_items_one_per_candidate_with_mermaid(tmp_path):
    manager = _Manager(
        [
            _wf_envelope("cand0.yaml", _ROS_A, index=0, name="高可用"),
            _wf_envelope("cand1.yaml", _ROS_B, index=1, name="经济型"),
        ]
    )
    items = diagram_items(manager, _session(tmp_path))
    assert {i["candidateIndex"] for i in items} == {0, 1}
    by_index = {i["candidateIndex"]: i for i in items}
    assert by_index[0]["candidateName"] == "高可用"
    # 架构图行徽标标注渲染格式(mermaid),而非源模板格式(yaml);与 a2a journal 约定一致。
    assert by_index[0]["format"] == "mermaid"
    assert by_index[1]["format"] == "mermaid"
    assert by_index[0]["mermaidSource"].startswith("graph TD")
    assert "MyVpc" in by_index[0]["mermaidSource"] or "VPC" in by_index[0]["mermaidSource"]


def test_diagram_items_mermaid_source_is_browser_safe(tmp_path):
    # 回归:ros_template_to_mermaid 产出的 subgraph 标题未加引号且含括号(如 "VPC (192.168.0.0/16)"),
    # 浏览器端 mermaid.js 会解析失败("Syntax error in text" 炸弹图)。diagram_items 需经
    # browser_mermaid_source 把标题转成加引号形态,与 HTML 预览(write_html)一致。
    manager = _Manager([_wf_envelope("cand0.yaml", _ROS_A, index=0, name="高可用")])
    items = diagram_items(manager, _session(tmp_path))
    source = items[0]["mermaidSource"]
    # 含带括号的 CIDR,证明确实走到了会触发原始语法错误的用例。
    assert "192.168.0.0/16" in source
    # 加引号的合法形态:subgraph <id>["..."]。
    assert 'subgraph layer_MyVpc["' in source
    # 不得残留未加引号的原始形态:subgraph <id> [...]。
    assert "subgraph layer_MyVpc [" not in source


def test_diagram_items_skips_non_ros_and_unparseable(tmp_path):
    manager = _Manager(
        [
            _wf_envelope("notes.txt", "hello", index=0),  # 非模板后缀
            _wf_envelope("bad.yaml", "Resources:\n  x: [", index=1),  # 坏 YAML
            _wf_envelope("plain.yaml", "foo: bar\n", index=2),  # 合法 YAML 但非 ROS 模板
        ]
    )
    assert diagram_items(manager, _session(tmp_path)) == []


def test_diagram_items_skips_template_without_resources(tmp_path):
    # 合法 ROS 模板但无 Resources:ros_template_to_mermaid 返回裸 "graph TD"(无节点),应跳过。
    manager = _Manager(
        [
            _wf_envelope("empty.yaml", "ROSTemplateFormatVersion: '2015-09-01'\n", index=0),
        ]
    )
    assert diagram_items(manager, _session(tmp_path)) == []


def test_diagram_items_dedupes_latest_per_candidate(tmp_path):
    manager = _Manager(
        [
            _wf_envelope("cand0.yaml", _ROS_A, index=0, name="v1"),
            _wf_envelope("cand0.yaml", _ROS_B, index=0, name="v2"),
        ]
    )
    items = diagram_items(manager, _session(tmp_path))
    assert len(items) == 1
    assert items[0]["candidateName"] == "v2"


def test_diagram_items_skips_non_candidate_write(tmp_path):
    # 回归:2 个候选模板却出现 3 张架构图。收尾/部署步会把选中候选的最终模板再写一次,
    # 该写入无 candidate 归属(index None),既无候选名又与已有候选图重复。应只留 2 张候选图,
    # 不得因此多出一张以裸绝对路径命名的图。
    manager = _Manager(
        [
            _wf_envelope("templates/1-clb-ha-web.yml", _ROS_A, index=0, name="CLB 经典负载均衡方案"),
            _wf_envelope("templates/2-alb-ha-web.yml", _ROS_B, index=1, name="ALB 应用型负载均衡方案"),
            # 收尾步重写选中候选的最终模板:无候选归属。
            _wf_envelope("templates/1-clb-ha-web.yml", _ROS_A),
            _wf_envelope("templates/1-clb-ha-web.yml", _ROS_A),
        ]
    )
    items = diagram_items(manager, _session(tmp_path))
    assert {i["candidateIndex"] for i in items} == {0, 1}
    # 无一条 candidateName 为空(空名会在前端渲染成裸路径)。
    assert all(i["candidateName"] for i in items)
    assert all(i["candidateIndex"] is not None for i in items)


def test_diagram_items_flags_optimizing_from_inflight(tmp_path):
    # 回归 step4 徽标倒退:优化进度态本只活在前端事件态,resync 会清空。协调器 _inflight 经
    # optimizing_indices 传入,后端权威 optimizing 标志让在途候选跨 resync 保持「优化中」。
    manager = _Manager(
        [
            _wf_envelope("cand0.yaml", _ROS_A, index=0, name="高可用"),
            _wf_envelope("cand1.yaml", _ROS_B, index=1, name="经济型"),
        ]
    )
    items = {i["candidateIndex"]: i for i in diagram_items(manager, _session(tmp_path), frozenset({0}))}
    assert items[0]["optimizing"] is True
    assert items[1]["optimizing"] is False


def test_diagram_items_optimizing_defaults_false(tmp_path):
    # 不传 optimizing_indices(默认空集)→ 旧调用者零回归,全部 optimizing=False。
    manager = _Manager([_wf_envelope("cand0.yaml", _ROS_A, index=0, name="x")])
    items = diagram_items(manager, _session(tmp_path))
    assert all(i["optimizing"] is False for i in items)


def test_outputs_payload_threads_optimizing_indices(tmp_path):
    manager = _Manager([_wf_envelope("cand0.yaml", _ROS_A, index=0, name="x")])
    manager.storage = SimpleNamespace(load=lambda cwd, sid: [])
    payload = outputs_payload(manager, _session(tmp_path), frozenset({0}))
    assert payload["diagrams"][0]["optimizing"] is True


def test_outputs_payload_includes_diagrams_key(tmp_path):
    # outputs_payload 需要 manager.storage.load;给个空消息的桩即可。
    manager = _Manager([_wf_envelope("cand0.yaml", _ROS_A, index=0, name="x")])
    manager.storage = SimpleNamespace(load=lambda cwd, sid: [])
    payload = outputs_payload(manager, _session(tmp_path))
    assert "diagrams" in payload
    assert payload["diagrams"][0]["candidateIndex"] == 0
    # get_outputs 直接透传 outputs_payload;锁死架构图行徽标格式为 mermaid(非源模板 yaml)。
    assert payload["diagrams"][0]["format"] == "mermaid"


def test_candidate_costs_maps_by_index(tmp_path):
    manager = _Manager(
        [
            _detail_envelope(0, [{"name": "ECS", "spec": "2c4g", "monthly_cost": "¥100"}], "¥100/月"),
            _detail_envelope(1, [{"name": "RDS", "spec": "1c2g", "monthly_cost": "¥50"}], "¥50/月"),
        ]
    )
    costs = pipeline_candidate_costs(manager, _session(tmp_path))
    assert set(costs) == {0, 1}
    assert costs[0]["totalMonthlyCost"] == "¥100/月"
    assert costs[0]["costItems"][0]["name"] == "ECS"


def test_candidate_costs_latest_wins_and_skips_missing_index(tmp_path):
    manager = _Manager(
        [
            _detail_envelope(0, [], "¥1/月"),
            _detail_envelope(0, [{"name": "ECS", "monthly_cost": "¥9"}], "¥9/月"),
            {"eventType": "candidate_detail_shown", "data": {"detail": {"costItems": [], "totalMonthlyCost": "x"}}},
        ]
    )
    costs = pipeline_candidate_costs(manager, _session(tmp_path))
    assert list(costs) == [0]
    assert costs[0]["totalMonthlyCost"] == "¥9/月"


def test_candidate_costs_empty_when_no_detail(tmp_path):
    manager = _Manager([_wf_envelope("c0.yaml", _ROS_A, index=0, name="x")])
    assert pipeline_candidate_costs(manager, _session(tmp_path)) == {}


def test_candidate_costs_from_completed_conclusion(tmp_path):
    # web/a2a 路径不调用 show_candidate_detail(confirm_and_select 的 inject_tools: []),
    # 询价只存在于 cost_estimating(step3)的 candidate_completed.conclusions.cost。
    manager = _Manager(
        [
            _completed_envelope(
                0,
                "经济型单机 Nginx 演示站",
                "¥46.12/月（列表价，合同优惠后约¥6.08/月）",
                [
                    {"type": "ECS 实例 ecs.s6-c1m1.small", "cost": "¥32.38/月"},
                    {"type": "云盘 40GiB", "cost": "¥8.00/月"},
                ],
            ),
        ]
    )
    costs = pipeline_candidate_costs(manager, _session(tmp_path))
    assert set(costs) == {0}
    assert costs[0]["totalMonthlyCost"] == "¥46.12/月（列表价，合同优惠后约¥6.08/月）"
    # resources[].{type,cost} → costItems[].{name,monthly_cost}
    assert costs[0]["costItems"][0] == {"name": "ECS 实例 ecs.s6-c1m1.small", "monthly_cost": "¥32.38/月"}
    assert costs[0]["costItems"][1] == {"name": "云盘 40GiB", "monthly_cost": "¥8.00/月"}


def test_candidate_costs_carries_pricing_calibers_from_conclusion(tmp_path):
    # cost.pricing_calibers.{planning_estimate,deviation_reason} → planningMonthlyEstimate / costCaliberNote,
    # 让前端把架构规划粗估与最终 ROS 估算并列展示。
    envelope = _completed_envelope(0, "x", "¥289.81/月", [{"type": "ECS", "cost": "¥32"}])
    envelope["data"]["conclusions"]["cost"]["pricing_calibers"] = {
        "planning_estimate": "¥300/月（粗略估算，列表价口径）",
        "list_price": "¥289.81/月",
        "calibers_aligned": True,
        "deviation_reason": "带宽假设由 5Mbps 调整为 1Mbps",
    }
    costs = pipeline_candidate_costs(_Manager([envelope]), _session(tmp_path))

    assert costs[0]["totalMonthlyCost"] == "¥289.81/月"
    assert costs[0]["planningMonthlyEstimate"] == "¥300/月（粗略估算，列表价口径）"
    assert costs[0]["costCaliberNote"] == "带宽假设由 5Mbps 调整为 1Mbps"


def test_candidate_costs_calibers_default_empty_without_reconciliation(tmp_path):
    manager = _Manager([_completed_envelope(0, "x", "¥46/月", [{"type": "ECS", "cost": "¥32"}])])
    costs = pipeline_candidate_costs(manager, _session(tmp_path))

    assert costs[0]["planningMonthlyEstimate"] == ""
    assert costs[0]["costCaliberNote"] == ""


def test_candidate_costs_carries_pricing_calibers_from_detail(tmp_path):
    envelope = _detail_envelope(0, [], "¥289.81/月")
    envelope["data"]["detail"]["planningMonthlyEstimate"] = "¥300/月"
    envelope["data"]["detail"]["costCaliberNote"] = "口径已对齐"
    costs = pipeline_candidate_costs(_Manager([envelope]), _session(tmp_path))

    assert costs[0]["planningMonthlyEstimate"] == "¥300/月"
    assert costs[0]["costCaliberNote"] == "口径已对齐"


def test_candidate_costs_detail_shown_wins_over_completed(tmp_path):
    # 同序号两种来源都在时,显式 show_candidate_detail(CLI)优先于 step3 结论。
    manager = _Manager(
        [
            _completed_envelope(0, "x", "¥46/月", [{"type": "ECS", "cost": "¥32"}]),
            _detail_envelope(0, [{"name": "RDS", "spec": "1c2g", "monthly_cost": "¥50"}], "¥50/月"),
        ]
    )
    costs = pipeline_candidate_costs(manager, _session(tmp_path))
    assert costs[0]["totalMonthlyCost"] == "¥50/月"
    assert costs[0]["costItems"][0]["name"] == "RDS"


def test_diagram_items_attaches_cost_by_index(tmp_path):
    manager = _Manager(
        [
            _wf_envelope("cand0.yaml", _ROS_A, index=0, name="高可用"),
            _wf_envelope("cand1.yaml", _ROS_B, index=1, name="经济型"),
            _detail_envelope(0, [{"name": "VPC", "monthly_cost": "¥0"}], "¥120/月"),
        ]
    )
    by_index = {i["candidateIndex"]: i for i in diagram_items(manager, _session(tmp_path))}
    # idx0 有 detail → 带价格
    assert by_index[0]["totalMonthlyCost"] == "¥120/月"
    assert by_index[0]["costItems"][0]["name"] == "VPC"
    # idx1 无 detail → 不含价格键(前端据此判「暂无询价」)
    assert "totalMonthlyCost" not in by_index[1]
    assert "costItems" not in by_index[1]


class _FakeManager:
    def __init__(self, envelopes):
        self._envelopes = envelopes

    def _load_a2a_pipeline_envelopes(self, context_id):
        return self._envelopes


def _tool_result(path, content, index, name):
    return {
        "eventType": "tool_result",
        "data": {"toolName": "write_file", "input": {"path": path, "content": content}},
        "candidate": {"index": index, "name": name},
    }


def test_iter_candidate_templates_yields_per_candidate(tmp_path):
    session = SimpleNamespace(cwd=str(tmp_path), context_id="ctx-1")
    manager = _FakeManager(
        [
            _tool_result("a.yaml", "ROSTemplateFormatVersion: '2015-09-01'\nA", 0, "经济极简"),
            _tool_result("b.yaml", "ROSTemplateFormatVersion: '2015-09-01'\nB", 1, "均衡"),
            {"eventType": "tool_result", "data": {"toolName": "read_file", "input": {}}},
            {"eventType": "status", "data": {}},
        ]
    )
    out = iter_candidate_templates(manager, session)
    assert [c.index for c in out] == [0, 1]
    assert isinstance(out[0], CandidateTemplate)
    assert out[0].name == "经济极简"
    assert out[0].template_content.endswith("A")
    assert out[1].source_rel_path == "b.yaml"


def test_iter_candidate_templates_latest_wins_per_index(tmp_path):
    session = SimpleNamespace(cwd=str(tmp_path), context_id="ctx-1")
    manager = _FakeManager(
        [
            _tool_result("a.yaml", "ROSTemplateFormatVersion: '2015-09-01'\nOLD", 0, "v1"),
            _tool_result("a.yaml", "ROSTemplateFormatVersion: '2015-09-01'\nNEW", 0, "v2"),
        ]
    )
    out = iter_candidate_templates(manager, session)
    assert len(out) == 1
    assert out[0].template_content.endswith("NEW")
    assert out[0].name == "v2"


def test_iter_candidate_templates_skips_indexless_writes(tmp_path):
    session = SimpleNamespace(cwd=str(tmp_path), context_id="ctx-1")
    manager = _FakeManager(
        [
            {
                "eventType": "tool_result",
                "data": {"toolName": "write_file", "input": {"path": "final.yaml", "content": "X"}},
            }
        ]
    )
    assert iter_candidate_templates(manager, session) == []


_TPL0 = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  V:\n    Type: ALIYUN::ECS::VPC\n"
_TPL1 = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  W:\n    Type: ALIYUN::ECS::VSwitch\n"


def test_diagram_items_prefers_cached_and_flags_optimized(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("iac_code.web.diagrams.pipeline_candidate_costs", lambda m, s: {})
    session = SimpleNamespace(cwd=str(tmp_path), context_id="ctx-1")
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0"), _tool_result("b.yaml", _TPL1, 1, "c1")])
    views = [{"id": "overview", "title": "总览", "mermaidSource": "graph TD\n  OPTIMIZED"}]
    dc.write_cached("ctx-1", 1, _TPL1, views, "m")

    items = {e["candidateIndex"]: e for e in diagram_items(manager, session)}
    assert items[1]["optimized"] is True
    assert items[1]["mermaidSource"] == views[0]["mermaidSource"]
    assert items[0]["optimized"] is False
    assert items[0]["mermaidSource"].startswith("graph")


def test_diagram_items_uses_cached_views(monkeypatch, tmp_path):
    # 命中缓存时,entry 暴露完整 views 列表;mermaidSource 取第一视图;optimized=True。
    # 未命中的候选不含 views 键,mermaidSource 为确定性草图,optimized=False。
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("iac_code.web.diagrams.pipeline_candidate_costs", lambda m, s: {})
    session = SimpleNamespace(cwd=str(tmp_path), context_id="ctx-1")
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0"), _tool_result("b.yaml", _TPL1, 1, "c1")])
    views = [
        {"id": "overview", "title": "总览", "mermaidSource": "graph TD\n  OVERVIEW"},
        {"id": "network", "title": "网络", "mermaidSource": "graph TD\n  NETWORK"},
    ]
    dc.write_cached("ctx-1", 1, _TPL1, views, "m")

    items = {e["candidateIndex"]: e for e in diagram_items(manager, session)}
    # 命中缓存的候选(index=1)
    assert items[1]["views"] == views
    assert len(items[1]["views"]) == 2
    assert items[1]["optimized"] is True
    assert items[1]["mermaidSource"] == views[0]["mermaidSource"]
    # 未命中的候选(index=0)
    assert "views" not in items[0]
    assert items[0]["optimized"] is False
    assert items[0]["mermaidSource"].startswith("graph")
