"""派生 web「输出面板」数据:资源栈与模板文件。纯派生,不新增持久化。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from iac_code.agent.message import Message, ToolUseBlock
from iac_code.web.session_manager import _tool_results_by_id

TEMPLATE_SUFFIXES = {".json", ".yaml", ".yml", ".tf"}
# 需要纳入面板的栈操作:含 DeleteStack,释放后应更新为 DELETE_COMPLETE 而非停留在旧状态
# (与同名去重配合:删除结果携带相同 region+栈名,会覆盖为最新状态)。
STACK_WRITE_ACTIONS = {"CreateStack", "UpdateStack", "ContinueCreateStack", "DeleteStack"}
# 流水线部署已从底层 ros_stack 迁移到编排工具 ros_deploy,其 action 是另一套命名
# (见 pipeline/selling/tools/ros_deploy_tool.py),结果 JSON 仍与 ros_stack 兼容
# (含 stack_id/stack_name/status/status_reason/is_success)。这些 action 均会产出/推进
# 一个栈的最终状态,故全部纳入面板派生;漏掉它们会导致 ros_deploy 部署的栈不再出现在输出面板。
ROS_DEPLOY_STACK_ACTIONS = {"create", "continue_create", "delete_and_create", "wait"}


def _is_stack_write_call(tool_name: Any, action: Any) -> bool:
    """判断一次工具调用是否为「应纳入资源栈面板」的栈写/部署操作。"""
    if tool_name == "ros_stack":
        return action in STACK_WRITE_ACTIONS
    if tool_name == "ros_deploy":
        return action in ROS_DEPLOY_STACK_ACTIONS
    return False


def _is_pending_stack_status(status: Any) -> bool:
    """判断栈状态是否为「进行中/已请求」的过渡态。

    用于在部署**开始**(而非完成)即把栈落进输出面板:底层栈工具内部轮询、只在**终态**
    才返回 ToolResult,故终态前面板一直空;而创建一开始就有 CREATE_IN_PROGRESS 的
    ``stack_current_changed`` 信封,据此过渡态提前入栈。终态仍由 tool_result 权威覆盖。
    """
    s = str(status or "").upper()
    return bool(s) and (s.endswith("_IN_PROGRESS") or s.endswith("_REQUESTED"))


_ROS_MARKERS = ("ROSTemplateFormatVersion", "Resources", "Transform")
_TF_PATTERN = re.compile(r'(^|\n)\s*(resource|provider|module|terraform)\s*["{]')


def build_ros_console_url(region_id: str | None, stack_id: str | None) -> str | None:
    """region 与 stack_id 均非空时返回 ROS 控制台栈详情 URL,否则 None。"""
    if not region_id or not stack_id:
        return None
    return "https://ros.console.aliyun.com/{}/stacks/{}".format(region_id, stack_id)


def template_format(suffix: str) -> str:
    """由扩展名推断预览格式:.json→json,.tf→terraform,其余→yaml。"""
    lowered = suffix.lower()
    if lowered == ".json":
        return "json"
    if lowered == ".tf":
        return "terraform"
    return "yaml"


def is_template_content(text: str, suffix: str) -> bool:
    """按内容宽松而稳健地判定是否为真·ROS/Terraform 模板;不确定返回 False。"""
    lowered = suffix.lower()
    if lowered == ".tf":
        return bool(_TF_PATTERN.search(text))
    if lowered == ".json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        return any(marker in data for marker in _ROS_MARKERS)
    # .yaml / .yml:轻量文本判定,避免 ROS 自定义标签反序列化开销
    return any(re.search(r"(^|\n)\s*{}\s*:".format(marker), text) for marker in _ROS_MARKERS)


def _default_region_id() -> str | None:
    """尽力获取默认阿里云 region;任何异常/缺省返回 None(测试可 monkeypatch)。"""
    try:
        from iac_code.services.cloud_credentials import CloudCredentials

        provider = CloudCredentials().get_provider("aliyun")
    except Exception:
        return None
    return getattr(provider, "region_id", None) or None


def _leading_json_object(text: str) -> dict[str, Any] | None:
    """从可能带尾随非 JSON 文本的字符串里解析开头的 JSON 对象,否则 None。

    ros_stack/ros_deploy 的结果 JSON 后常被 ``attach_ros_validation`` 追加一段
    ``\\n\\n---\\nROS local preflight diagnostics:\\n...`` 本地预检诊断块(见
    tools/cloud/aliyun/ros_validation/outcome.py),使整体不再是合法 JSON。若用严格
    ``json.loads`` 会整条失败,create/continue_create/delete_and_create 的权威终态因此
    被丢弃——面板只剩更早捕获的过渡态(可能是已删除的旧栈),即「永远只显示之前删除的那个 stack」。
    这里用 ``raw_decode`` 只解析开头对象、容忍其后任意文本。
    """
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _stack_result_dict(result: Any) -> dict[str, Any] | None:
    """把单个 ros_stack 结果(str 或 dict)解析为含 stack_id 的字典,否则 None。"""
    if isinstance(result, dict):
        data: dict[str, Any] | None = result
    elif isinstance(result, str):
        data = _leading_json_object(result)
    else:
        return None
    if isinstance(data, dict) and data.get("stack_id"):
        return data
    return None


def _stack_result_json(results: list[Any]) -> dict[str, Any] | None:
    for block in results:
        data = _stack_result_dict(getattr(block, "content", None))
        if data:
            return data
    return None


def pipeline_candidate_options(manager: Any, session: Any) -> list[dict[str, Any]]:
    """从 pipeline A2A 日志取权威候选表(confirm_and_select 的 ``input_required.options``)。

    候选清单必须来自权威提问信封,而非「架构图能否渲染」——某候选模板解析失败时
    仍应可选,只是缺「查看架构图」。取**最后一个** ``eventType=="input_required"`` 且
    其 ``data.options`` 里带 ``candidate_index`` 的信封(据此区别于 ask_user_question),
    返回按 candidateIndex 升序的 ``[{candidateName, candidateIndex, summary}]``;找不到返回 []。
    """
    latest_options: list[Any] | None = None
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        if envelope.get("eventType") != "input_required":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        options = data.get("options")
        if not isinstance(options, list):
            continue
        if not any(isinstance(opt, dict) and "candidate_index" in opt for opt in options):
            continue
        latest_options = options

    if latest_options is None:
        return []

    candidates: list[dict[str, Any]] = []
    for opt in latest_options:
        if not isinstance(opt, dict) or "candidate_index" not in opt:
            continue
        candidates.append(
            {
                "candidateName": opt.get("name") or "",
                "candidateIndex": opt.get("candidate_index"),
                "summary": opt.get("summary") or "",
            }
        )
    candidates.sort(key=lambda item: (item["candidateIndex"] is None, item["candidateIndex"]))
    return candidates


def pipeline_candidate_costs(manager: Any, session: Any) -> dict[int, dict[str, Any]]:
    """从 pipeline A2A 日志取各候选的询价,按候选序号聚合。

    两个来源(与 diagram_items 同一 loader,全量信封,重载安全):

    - ``candidate_completed``(cost_estimating / step3 的结论):web/a2a 路径的**唯一**来源。
      confirm_and_select 在 a2a surface 用 ``inject_tools: []`` 剥掉了 show_candidate_detail,
      故 web 不会有 ``candidate_detail_shown``;询价存在于 ``data.conclusions.cost``
      (``monthly_estimate`` + ``resources:[{type, cost}]``)。映射为
      ``totalMonthlyCost`` / ``costItems:[{name, monthly_cost}]``。
    - ``candidate_detail_shown``(CLI 显式 show_candidate_detail):保留兼容,携带
      ``detail.costItems`` / ``detail.totalMonthlyCost``。

    候选序号取 ``data.candidateIndex``(detail 回退 ``data.detail.candidateIndex``);缺序号
    无法与架构图对齐,跳过。按信封顺序 latest-wins;journal 中 completed 早于 detail,故同序号
    两者都在时 detail 优先。返回 ``{index: {costItems, totalMonthlyCost}}``。
    """
    costs: dict[int, dict[str, Any]] = {}
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        event_type = envelope.get("eventType")
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "candidate_completed":
            index = data.get("candidateIndex")
            if not isinstance(index, int):
                continue
            conclusions = data.get("conclusions")
            cost = conclusions.get("cost") if isinstance(conclusions, dict) else None
            if not isinstance(cost, dict):
                continue
            resources = cost.get("resources")
            items = (
                [
                    {"name": res.get("type") or "", "monthly_cost": res.get("cost") or ""}
                    for res in resources
                    if isinstance(res, dict)
                ]
                if isinstance(resources, list)
                else []
            )
            total = cost.get("monthly_estimate")
            costs[index] = {
                "costItems": items,
                "totalMonthlyCost": total if isinstance(total, str) else "",
            }
        elif event_type == "candidate_detail_shown":
            detail = data.get("detail")
            detail = detail if isinstance(detail, dict) else {}
            index = data.get("candidateIndex")
            if index is None:
                index = detail.get("candidateIndex")
            if not isinstance(index, int):
                continue
            items = detail.get("costItems")
            total = detail.get("totalMonthlyCost")
            costs[index] = {
                "costItems": items if isinstance(items, list) else [],
                "totalMonthlyCost": total if isinstance(total, str) else "",
            }
    return costs


def outputs_payload(manager: Any, session: Any, optimizing_indices: frozenset[int] = frozenset()) -> dict[str, Any]:
    """扫描会话已存储消息 + pipeline A2A 日志,派生资源栈与模板文件列表。

    optimizing_indices 透传给 diagram_items,把协调器在途优化态挂到架构图的后端权威 optimizing 标志上。
    """
    messages: list[Message] = manager.storage.load(session.cwd, session.session_id)
    results_by_id = _tool_results_by_id(messages)
    cwd = Path(session.cwd).expanduser().resolve()

    stacks: dict[str, dict[str, Any]] = {}
    files: dict[str, dict[str, Any]] = {}
    default_region: dict[str, str | None] = {"value": None}

    def resolve_region(raw_region: Any) -> str:
        region = str(raw_region or "")
        if not region:
            if default_region["value"] is None:
                default_region["value"] = _default_region_id() or ""
            region = default_region["value"] or ""
        return region

    def add_stack(data: dict[str, Any], raw_region: Any) -> None:
        region = resolve_region(raw_region)
        stack_id = str(data.get("stack_id"))
        stack_name = str(data.get("stack_name") or "")
        # 同名栈按「region + 栈名」去重(失败后重试 CreateStack 会生成新 stack_id,
        # 但对用户是同一个栈),仅保留最新一次状态;无栈名时退回 stack_id 以免误并。
        key = "{}::{}".format(region, stack_name) if stack_name else stack_id
        stacks[key] = {
            "stackId": stack_id,
            "stackName": stack_name or stack_id,
            "status": data.get("status") or "",
            "statusReason": data.get("status_reason") or "",
            "isSuccess": bool(data.get("is_success")),
            "regionId": region,
            "consoleUrl": build_ros_console_url(region, stack_id),
        }

    def add_file(raw_path: Any, captured: str | None) -> None:
        if not raw_path:
            return
        suffix = Path(str(raw_path)).suffix.lower()
        if suffix not in TEMPLATE_SUFFIXES:
            return
        abs_path = (cwd / str(raw_path)).resolve()
        # 优先用日志/消息捕获的内容判定;判不出再回退磁盘(应对截断内容或 edit_file)。
        candidates: list[str] = []
        if captured is not None:
            candidates.append(captured)
        try:
            candidates.append(abs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not any(is_template_content(text, suffix) for text in candidates):
            return
        try:
            # Use POSIX separators so the web API path contract is stable
            # across platforms (Windows relative_to() would emit backslashes).
            rel = abs_path.relative_to(cwd).as_posix()
        except ValueError:
            rel = abs_path.name
        files[str(abs_path)] = {
            "path": str(abs_path),
            "name": abs_path.name,
            "format": template_format(suffix),
            "relPath": rel,
        }

    for message in messages:
        if isinstance(message.content, str):
            continue
        for block in message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            if _is_stack_write_call(block.name, block.input.get("action")):
                data = _stack_result_json(results_by_id.get(block.id, []))
                if data:
                    add_stack(data, block.input.get("region_id"))
            elif block.name in {"write_file", "edit_file"}:
                add_file(block.input.get("path"), None)

    # pipeline 的工具调用记在独立 A2A 子会话日志里(主会话 jsonl 只有用户 prompt),
    # 需按 contextId 读取 envelope 才能识别其生成的模板与部署的栈。
    for envelope in manager._load_a2a_pipeline_envelopes(getattr(session, "context_id", None)):
        event_type = envelope.get("eventType")
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        if event_type == "stack_current_changed":
            # 部署一开始(CreateStack 刚返回 stack_id)即落一个「进行中」栈,让输出面板在
            # 创建开始就出现资源栈,而非等到终态 tool_result(可能是数分钟后)。终态到来时
            # 下方 tool_result 分支会以相同 region::栈名 键、用权威结果(status_reason/is_success)覆盖之。
            if _is_pending_stack_status(data.get("stackStatus")) and data.get("stackId"):
                add_stack(
                    {
                        "stack_id": data.get("stackId"),
                        "stack_name": data.get("stackName") or "",
                        "status": data.get("stackStatus"),
                        "status_reason": "",
                        "is_success": False,
                    },
                    data.get("regionId"),
                )
            continue
        if event_type != "tool_result":
            continue
        tool_name = data.get("toolName")
        tool_input = data.get("input") or {}
        if _is_stack_write_call(tool_name, tool_input.get("action")):
            parsed = _stack_result_dict(data.get("result"))
            if parsed:
                add_stack(parsed, tool_input.get("region_id"))
        elif tool_name in {"write_file", "edit_file"}:
            add_file(tool_input.get("path"), tool_input.get("content"))

    from iac_code.web.diagrams import diagram_items

    return {
        "stacks": list(stacks.values()),
        "files": list(files.values()),
        "diagrams": diagram_items(manager, session, optimizing_indices),
        "candidates": pipeline_candidate_options(manager, session),
    }


class OutputPathForbidden(Exception):  # noqa: N818
    """请求的文件路径越出会话 cwd。"""


class OutputFileMissing(Exception):  # noqa: N818
    """请求的文件不存在或不可读。"""


def read_output_file(session: Any, rel_or_abs_path: str, *, allowed_paths: set[str] | None = None) -> dict[str, Any]:
    """读取单个文件用于预览;越界/缺失分别抛出可转 403/404 的异常。

    默认只允许会话 cwd 内的文件;``allowed_paths`` 给出的绝对路径(通常来自
    ``outputs_payload`` 派生的输出集)即使在 cwd 之外也放行——应对 agent 把模板
    写到 /tmp 等 cwd 外位置的情形,同时不放开任意路径穿越。
    """
    cwd = Path(session.cwd).expanduser().resolve()
    candidate = Path(rel_or_abs_path)
    target = candidate if candidate.is_absolute() else cwd / candidate
    resolved = target.resolve()

    cwd_str = str(cwd)
    resolved_str = str(resolved)
    in_cwd = resolved_str == cwd_str or resolved_str.startswith(cwd_str + os.sep)
    if not in_cwd and not (allowed_paths and resolved_str in allowed_paths):
        raise OutputPathForbidden(rel_or_abs_path)

    try:
        content = resolved.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise OutputFileMissing(rel_or_abs_path) from exc

    try:
        rel = str(resolved.relative_to(cwd))
    except ValueError:
        # cwd 之外的文件用绝对路径展示,如实反映其真实位置。
        rel = resolved_str
    return {"path": rel, "content": content, "format": template_format(resolved.suffix)}
