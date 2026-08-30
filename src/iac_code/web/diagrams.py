"""派生 web「架构图」输出:从候选模板生成 mermaid。纯派生,不新增持久化,不触碰 a2a 逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iac_code.pipeline.engine.architecture_semantic_planning import browser_mermaid_source
from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid
from iac_code.web.diagram_cache import read_cached
from iac_code.web.outputs import TEMPLATE_SUFFIXES, is_template_content, pipeline_candidate_costs

_MATERIALIZED_STEP_ID = "materialize_selected_candidate"
_MATERIALIZED_OPTIMIZATION_KEY = "materialized"
_ARCHITECTURE_PLAN_SOURCE = "architecture_plan"


def _mermaid_or_none(content: str, suffix: str) -> str | None:
    """仅对 ROS YAML 模板产出 mermaid;非 YAML/非模板/解析失败一律 None。"""
    if suffix not in {".yaml", ".yml"}:
        return None
    if not is_template_content(content, suffix):
        return None
    try:
        source = ros_template_to_mermaid(content)
    except Exception:
        return None
    if not source:
        return None
    # ros_template_to_mermaid 不抛异常:YAML 解析失败时返回哨兵图(节点 id 恒为 Error,
    # 仅括号内文案随 i18n 变化),据此判定为不可解析并跳过。
    if source.startswith("graph TD\n  Error["):
        return None
    # 非 dict / 无 Resources 时 ros_template_to_mermaid 只返回裸表头(无节点),视为空图跳过。
    if source.strip() == "graph TD":
        return None
    # ros_template_to_mermaid 产出的 subgraph 标题未加引号且可能含括号(如 "VPC (10.0.0.0/16)"),
    # 浏览器端 mermaid.js 会解析失败(炸弹图)。复用与 HTML 预览(write_html)同一转换,给标题加引号。
    return browser_mermaid_source(source)


def _read_content(cwd: Path, raw_path: Any, captured: str | None) -> tuple[str, str] | None:
    """返回 (content, suffix);优先用捕获内容,回退磁盘;取不到返回 None。"""
    if not raw_path:
        return None
    suffix = Path(str(raw_path)).suffix.lower()
    if suffix not in TEMPLATE_SUFFIXES:
        return None
    if captured is not None:
        return captured, suffix
    try:
        return (cwd / str(raw_path)).resolve().read_text(encoding="utf-8"), suffix
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class CandidateTemplate:
    """某候选(index)最新一次生成的模板;不因草图能否渲染而丢弃。"""

    index: int
    name: str
    template_content: str
    suffix: str
    source_rel_path: str


def iter_candidate_templates(manager: Any, session: Any) -> list[CandidateTemplate]:
    """枚举 pipeline journal 里各候选生成的模板(按 index 去重,保留最新)。"""
    cwd = Path(session.cwd).expanduser().resolve()
    latest: dict[int, CandidateTemplate] = {}
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        if envelope.get("eventType") != "tool_result":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("toolName") not in {"write_file", "edit_file"}:
            continue
        tool_input = data.get("input") or {}
        read = _read_content(cwd, tool_input.get("path"), tool_input.get("content"))
        if read is None:
            continue
        content, suffix = read
        candidate = envelope.get("candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        index = candidate.get("index")
        if index is None:
            continue
        latest[index] = CandidateTemplate(
            index=index,
            name=candidate.get("name") or "",
            template_content=content,
            suffix=suffix,
            source_rel_path=str(tool_input.get("path") or ""),
        )
    return list(latest.values())


def _materialized_costs(manager: Any, session: Any) -> dict[str, dict[str, Any]]:
    """Return the latest exact Step 2 quote keyed by canonical template path.

    ``selling_solution_first`` publishes the Python-normalized quote in its
    deployment-confirmation ``input_required`` envelope.  That is the same public
    value used by the confirmation UI, so the architecture preview must not parse
    raw ROS responses again or trust model-authored text.
    """

    cwd = Path(session.cwd).expanduser().resolve()
    costs: dict[str, dict[str, Any]] = {}
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        if envelope.get("eventType") != "input_required":
            continue
        step = envelope.get("step")
        step = step if isinstance(step, dict) else {}
        data = envelope.get("data")
        if (
            not isinstance(data, dict)
            or str(step.get("id") or data.get("stepId") or "") != _MATERIALIZED_STEP_ID
            or data.get("kind") != "deployment_confirmation"
        ):
            continue
        template_url = data.get("template_url")
        cost = data.get("cost")
        if not template_url or not isinstance(cost, dict):
            continue
        raw_path = Path(str(template_url))
        canonical = str((raw_path if raw_path.is_absolute() else cwd / raw_path).resolve())
        resources = cost.get("resources")
        items = []
        if isinstance(resources, list):
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                item: dict[str, str] = {
                    "name": str(resource.get("type") or resource.get("name") or ""),
                    "monthly_cost": str(resource.get("cost") or resource.get("monthly_cost") or ""),
                }
                if resource.get("spec"):
                    item["spec"] = str(resource["spec"])
                items.append(item)
        total = cost.get("monthly_estimate")
        costs[canonical] = {
            "costItems": items,
            "totalMonthlyCost": total if isinstance(total, str) else "",
        }
    return costs


def diagram_items(
    manager: Any,
    session: Any,
    optimizing_indices: frozenset[int | str] = frozenset(),
) -> list[dict[str, Any]]:
    """扫描 pipeline A2A envelope 里的规划图与候选模板，产出架构图列表。

    optimizing_indices:当前仍在后台优化的候选 index(来自协调器 _inflight)。优化进度态本只活在前端
    事件归约态,resync 会清空;把它挂到后端权威 optimizing 标志上,徽标才能跨 resync 不倒退成「待优化」。
    """
    cwd = Path(session.cwd).expanduser().resolve()
    by_key: dict[str, dict[str, Any]] = {}
    costs = pipeline_candidate_costs(manager, session)
    materialized_costs = _materialized_costs(manager, session)
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        if envelope.get("eventType") == "diagram_shown":
            data = envelope.get("data")
            data = data if isinstance(data, dict) else {}
            architecture_context = data.get("architectureContext")
            architecture_context = architecture_context if isinstance(architecture_context, dict) else {}
            index = data.get("candidateIndex")
            source = data.get("mermaidSource")
            # selling_solution_first Step 1 没有 ROS 模板，show_architecture_plan 直接发出
            # template-less DiagramEvent。只接受工具写入的显式 source 标记，避免改变旧 selling
            # 及其他 diagram_shown 事件的输出集合；同一候选多轮规划按日志顺序保留最新图。
            if (
                architecture_context.get("source") == _ARCHITECTURE_PLAN_SOURCE
                and isinstance(index, int)
                and not isinstance(index, bool)
                and isinstance(source, str)
                and source.strip()
            ):
                raw_views = data.get("views")
                views: list[dict[str, Any]] = []
                if isinstance(raw_views, list):
                    for raw_view in raw_views:
                        if not isinstance(raw_view, dict):
                            continue
                        view_source = raw_view.get("mermaidSource") or raw_view.get("mermaid_source")
                        if not isinstance(view_source, str) or not view_source.strip():
                            continue
                        views.append(
                            {
                                "id": str(raw_view.get("id") or "overview"),
                                "title": str(raw_view.get("title") or ""),
                                "purpose": str(raw_view.get("purpose") or ""),
                                "mermaidSource": view_source,
                            }
                        )
                stage = str(data.get("diagramStage") or "optimized")
                entry: dict[str, Any] = {
                    "diagramId": str(data.get("diagramId") or envelope.get("eventId") or f"plan:{index}"),
                    "candidateName": str(data.get("candidateName") or ""),
                    "candidateIndex": index,
                    "format": "mermaid",
                    "mermaidSource": source,
                    "optimized": stage == "optimized",
                    "optimizing": False,
                    "diagramStage": stage,
                    "architectureContext": architecture_context,
                }
                if views:
                    entry["views"] = views
                cost = costs.get(index)
                if cost is not None:
                    entry["costItems"] = cost["costItems"]
                    entry["totalMonthlyCost"] = cost["totalMonthlyCost"]
                by_key[str(index)] = entry
            continue
        if envelope.get("eventType") != "tool_result":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("toolName") not in {"write_file", "edit_file"}:
            continue
        tool_input = data.get("input") or {}
        read = _read_content(cwd, tool_input.get("path"), tool_input.get("content"))
        if read is None:
            continue
        content, suffix = read
        source = _mermaid_or_none(content, suffix)
        if source is None:
            continue
        candidate = envelope.get("candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        index = candidate.get("index")
        # 旧 selling 的无候选归属写入是候选模板的重复副本，继续跳过。新
        # selling_solution_first 的 Step 2 则第一次产生真实 ROS 模板；仅为这个唯一
        # step 派生最终架构图，避免按 pipeline 名称分支或改变旧流程的输出集合。
        if index is None:
            step = envelope.get("step")
            step = step if isinstance(step, dict) else {}
            step_id = str(step.get("id") or data.get("stepId") or "")
            if step_id != _MATERIALIZED_STEP_ID:
                continue
            raw_path = Path(str(tool_input.get("path") or ""))
            resolved_path = (raw_path if raw_path.is_absolute() else cwd / raw_path).resolve()
            try:
                rel = resolved_path.relative_to(cwd).as_posix()
            except ValueError:
                rel = str(raw_path)
            canonical_key = str(resolved_path)
            cached = read_cached(
                getattr(session, "context_id", None),
                _MATERIALIZED_OPTIMIZATION_KEY,
                content,
            )
            entry: dict[str, Any] = {
                "diagramId": f"final:{rel}",
                "candidateName": "",
                "candidateIndex": None,
                "format": "mermaid",
                "mermaidSource": cached[0]["mermaidSource"] if cached else source,
                "optimized": cached is not None,
                "optimizing": _MATERIALIZED_OPTIMIZATION_KEY in optimizing_indices,
                "optimizationKey": _MATERIALIZED_OPTIMIZATION_KEY,
                "sourceRelPath": rel,
                "stepId": step_id,
                "diagramStage": "optimized" if cached else "draft",
            }
            if cached:
                entry["views"] = cached
            exact_cost = materialized_costs.get(canonical_key)
            if exact_cost is not None:
                entry.update(exact_cost)
            by_key[f"final:{canonical_key}"] = entry
            continue
        name = candidate.get("name")
        rel = str(tool_input.get("path") or "")
        key = str(index)
        cached = read_cached(getattr(session, "context_id", None), index, content)
        entry = {
            "diagramId": "{}:{}".format(index, rel),
            "candidateName": name,
            "candidateIndex": index,
            # 该行是架构图(渲染形态恒为 mermaid),徽标标注渲染格式而非源模板格式;
            # 与 a2a journal 的 DiagramEvent(pipeline_events.py:1416 format:"mermaid")约定一致。
            "format": "mermaid",
            "mermaidSource": cached[0]["mermaidSource"] if cached else source,
            "optimized": cached is not None,
            "optimizing": index in optimizing_indices,
            "sourceRelPath": rel,
        }
        if cached:
            entry["views"] = cached
        cost = costs.get(index)
        if cost is not None:
            entry["costItems"] = cost["costItems"]
            entry["totalMonthlyCost"] = cost["totalMonthlyCost"]
        by_key[key] = entry
    return list(by_key.values())
