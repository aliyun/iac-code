import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import iac_code.web.diagram_cache as dc
import iac_code.web.diagram_optimizer as opt
from iac_code.web.runtime import WebModelSelection

_TPL0 = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  V:\n    Type: ALIYUN::ECS::VPC\n"
_TPL1 = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  W:\n    Type: ALIYUN::ECS::VSwitch\n"


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


def _confirm_and_select_envelope():
    return {
        "eventType": "input_required",
        "data": {"options": [{"candidate_index": 0}, {"candidate_index": 1}]},
    }


def _make_session(tmp_path):
    return SimpleNamespace(
        cwd=str(tmp_path),
        context_id="ctx-1",
        active_local_tasks=set(),
        events=SimpleNamespace(publish=AsyncMock()),
    )


class _V:
    def __init__(self, vid, title, src):
        self.id, self.title, self.mermaid_source = vid, title, src


class _Multi:
    def __init__(self, views):
        self.views = tuple(views)


def _patch_engine(monkeypatch, plan_spy=None):
    # Callers set dc.get_config_dir -> tmp_path themselves right after this.
    monkeypatch.setattr(
        opt,
        "render_ros_template_architecture",
        lambda tpl, *, semantic_plan=None: SimpleNamespace(
            mermaid_source="graph TD\n  X-->Y", architecture_context={"visible_nodes": []}
        ),
    )
    monkeypatch.setattr(
        opt,
        "render_ros_template_architecture_views",
        lambda tpl, *, semantic_plan=None: _Multi(
            (_V("overview", "总览", "graph TD\n A-->B"), _V("detail_net", "网络层", "graph TD\n C-->D"))
        ),
    )
    monkeypatch.setattr(opt, "browser_mermaid_source", lambda src: "BROWSER:" + src)

    async def _fake_plan(architecture_context, template_content, **kwargs):
        if plan_spy is not None:
            plan_spy.append(kwargs)
        return {"node_labels": [{"id": "X", "label": "x"}]}

    monkeypatch.setattr(opt, "create_semantic_plan_for_architecture_with_llm", _fake_plan)


@pytest.mark.asyncio
async def test_optimize_publishes_writes_cache_and_threads_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    plan_spy: list = []
    _patch_engine(monkeypatch, plan_spy)
    write_spy: list = []
    _real_write = dc.write_cached

    def _spy_write(context_id, candidate_index, template_content, views, model):
        write_spy.append(views)
        _real_write(context_id, candidate_index, template_content, views, model)

    monkeypatch.setattr(opt, "write_cached", _spy_write)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(
            provider="qwenpaw",
            model="qwen3.6-plus",
            effort="high",
            provider_api_key="k",
            provider_base_url="http://x",
            provider_config_frozen=True,
            provider_config_override={"apiBase": "http://x"},
        ),
    )
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0"), _tool_result("b.yaml", _TPL1, 1, "c1")])

    coord = opt.DiagramOptimizationCoordinator()
    coord.maybe_trigger(session, manager, _confirm_and_select_envelope())
    await asyncio.gather(*list(session.active_local_tasks))

    published = [c.args for c in session.events.publish.call_args_list]
    types = [t for (t, _payload) in published]
    assert types.count("diagram.optimizing") == 2
    assert types.count("diagram.optimized") == 2
    done = [p for (t, p) in published if t == "diagram.optimized"]
    assert all(p["status"] == "done" for p in done)
    for p in done:
        assert len(p["views"]) == 2
        for v in p["views"]:
            assert set(v) == {"id", "title", "mermaidSource"}
            assert v["mermaidSource"].startswith("BROWSER:")
        assert p["views"][0] == {"id": "overview", "title": "总览", "mermaidSource": "BROWSER:graph TD\n A-->B"}
        assert p["views"][1] == {"id": "detail_net", "title": "网络层", "mermaidSource": "BROWSER:graph TD\n C-->D"}
        assert p["mermaidSource"] == p["views"][0]["mermaidSource"]

    assert len(write_spy) == 2
    assert all(len(v) == 2 for v in write_spy)

    cached0 = dc.read_cached("ctx-1", 0, _TPL0)
    assert cached0 is not None and len(cached0) == 2
    assert cached0[0]["mermaidSource"] == "BROWSER:graph TD\n A-->B"
    cached1 = dc.read_cached("ctx-1", 1, _TPL1)
    assert cached1 is not None and len(cached1) == 2

    kw = plan_spy[0]
    assert kw["provider_key_override"] == "qwenpaw"
    assert kw["credentials_override"] == {"qwenpaw": "k"}
    assert kw["base_url_override"] == "http://x"
    assert kw["provider_config_override"] == {"apiBase": "http://x"}
    assert kw["ignore_llm_source"] is True
    assert kw["effort_override"] == "none"


@pytest.mark.asyncio
async def test_maybe_trigger_skips_when_cached(monkeypatch, tmp_path):
    plan_spy: list = []
    _patch_engine(monkeypatch, plan_spy)
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(provider=None, model="m", effort=None),
    )
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0")])
    dc.write_cached("ctx-1", 0, _TPL0, [{"id": "overview", "title": "", "mermaidSource": "graph TD\n  CACHED"}], "m")

    coord = opt.DiagramOptimizationCoordinator()
    coord.maybe_trigger(session, manager, _confirm_and_select_envelope())
    await asyncio.gather(*list(session.active_local_tasks))

    assert plan_spy == []  # cache hit -> no LLM call
    assert session.events.publish.await_count == 0


@pytest.mark.asyncio
async def test_non_candidate_input_required_ignored(monkeypatch, tmp_path):
    plan_spy: list = []
    _patch_engine(monkeypatch, plan_spy)
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(provider=None, model="m", effort=None),
    )
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0")])

    coord = opt.DiagramOptimizationCoordinator()
    # ask_user_question style: options without candidate_index
    coord.maybe_trigger(session, manager, {"eventType": "input_required", "data": {"options": [{"id": "yes"}]}})
    await asyncio.gather(*list(session.active_local_tasks))
    assert plan_spy == []
    assert session.active_local_tasks == set()


@pytest.mark.asyncio
async def test_optimize_failure_publishes_failed_and_skips_cache(monkeypatch, tmp_path):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(provider=None, model="m", effort=None),
    )

    async def _boom(architecture_context, template_content, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(opt, "create_semantic_plan_for_architecture_with_llm", _boom)
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0")])

    coord = opt.DiagramOptimizationCoordinator()
    coord.maybe_trigger(session, manager, _confirm_and_select_envelope())
    await asyncio.gather(*list(session.active_local_tasks))

    published = [c.args for c in session.events.publish.call_args_list]
    failed = [p for (t, p) in published if t == "diagram.optimized"]
    assert failed and failed[0]["status"] == "failed"
    assert dc.read_cached("ctx-1", 0, _TPL0) is None


@pytest.mark.asyncio
async def test_views_render_raises_publishes_failed_and_skips_cache(monkeypatch, tmp_path):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(provider=None, model="m", effort=None),
    )

    def _boom_views(tpl, *, semantic_plan=None):
        raise RuntimeError("render blew up")

    monkeypatch.setattr(opt, "render_ros_template_architecture_views", _boom_views)
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0")])

    coord = opt.DiagramOptimizationCoordinator()
    coord.maybe_trigger(session, manager, _confirm_and_select_envelope())
    await asyncio.gather(*list(session.active_local_tasks))

    published = [c.args for c in session.events.publish.call_args_list]
    failed = [p for (t, p) in published if t == "diagram.optimized"]
    assert failed and failed[0]["status"] == "failed"
    assert dc.read_cached("ctx-1", 0, _TPL0) is None


@pytest.mark.asyncio
async def test_all_views_filtered_publishes_failed_and_skips_cache(monkeypatch, tmp_path):
    _patch_engine(monkeypatch)
    monkeypatch.setattr(dc, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        opt,
        "model_selection_for_session",
        lambda session: WebModelSelection(provider=None, model="m", effort=None),
    )
    monkeypatch.setattr(
        opt,
        "render_ros_template_architecture_views",
        lambda tpl, *, semantic_plan=None: _Multi((_V("overview", "总览", "graph TD"), _V("detail", "细节", ""))),
    )
    session = _make_session(tmp_path)
    manager = _FakeManager([_tool_result("a.yaml", _TPL0, 0, "c0")])

    coord = opt.DiagramOptimizationCoordinator()
    coord.maybe_trigger(session, manager, _confirm_and_select_envelope())
    await asyncio.gather(*list(session.active_local_tasks))

    published = [c.args for c in session.events.publish.call_args_list]
    failed = [p for (t, p) in published if t == "diagram.optimized"]
    assert failed and failed[0]["status"] == "failed"
    assert dc.read_cached("ctx-1", 0, _TPL0) is None


def test_optimizing_indices_filters_by_context():
    # /outputs 据此把在途优化态挂到架构图的后端权威 optimizing 标志上,跨 resync 不倒退。
    coord = opt.DiagramOptimizationCoordinator()
    coord._inflight.add(("ctx-1", 0))
    coord._inflight.add(("ctx-1", 2))
    coord._inflight.add(("ctx-2", 5))
    assert coord.optimizing_indices("ctx-1") == {0, 2}
    assert coord.optimizing_indices("ctx-2") == {5}
    assert coord.optimizing_indices("other") == set()
    assert coord.optimizing_indices(None) == set()
    assert coord.optimizing_indices("") == set()
