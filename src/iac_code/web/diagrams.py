"""派生 web「架构图」输出:从候选模板生成 mermaid。纯派生,不新增持久化,不触碰 a2a 逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iac_code.pipeline.engine.architecture_semantic_planning import browser_mermaid_source
from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid
from iac_code.web.diagram_cache import read_cached
from iac_code.web.outputs import TEMPLATE_SUFFIXES, is_template_content, pipeline_candidate_costs


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


def diagram_items(manager: Any, session: Any, optimizing_indices: frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    """扫描 pipeline A2A envelope 里各候选生成的模板,产出架构图列表(按候选去重,保留最新)。

    optimizing_indices:当前仍在后台优化的候选 index(来自协调器 _inflight)。优化进度态本只活在前端
    事件归约态,resync 会清空;把它挂到后端权威 optimizing 标志上,徽标才能跨 resync 不倒退成「待优化」。
    """
    cwd = Path(session.cwd).expanduser().resolve()
    by_key: dict[str, dict[str, Any]] = {}
    costs = pipeline_candidate_costs(manager, session)
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
        source = _mermaid_or_none(content, suffix)
        if source is None:
            continue
        candidate = envelope.get("candidate")
        candidate = candidate if isinstance(candidate, dict) else {}
        index = candidate.get("index")
        # 架构图仅呈现「各候选生成的模板」。无候选归属的写入(index None,如收尾/部署步把选中
        # 候选的最终模板再写一次)既无候选名、又与已有候选图重复,若收录会以裸绝对路径命名多出一张,
        # 故跳过——只保留候选作用域内的模板写入。
        if index is None:
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
            if "costEstimateVerified" in cost:
                entry["costEstimateVerified"] = cost["costEstimateVerified"]
            if "unverifiedReason" in cost:
                entry["unverifiedReason"] = cost["unverifiedReason"]
        by_key[key] = entry
    return list(by_key.values())
