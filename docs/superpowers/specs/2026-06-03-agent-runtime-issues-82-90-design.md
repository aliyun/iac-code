# Agent Runtime Issues #82 and #90 Design

## 背景

本设计同时解决 GitHub issue #82 和 #90。两者都属于 agent runtime 的状态边界问题：

- #82：context compaction 使用 `preserve_recent_turns * 2` 作为消息切分点，可能把 assistant 的 `tool_use` 和 user 的 `tool_result` 拆开，生成对模型无效或容易混淆的上下文。
- #90：`AgentTool` 的后台任务没有被持有和取消；同时 sub-agent progress queue 保存在共享 `AgentTool` 实例字段 `_event_queue` 上，并发调用时会互相覆盖。

目标是修掉根因，同时保持改动局限在已有模块边界内：`ContextManager` 负责 compaction 边界，`ToolExecutor` 负责 per-call tool context，`AgentTool` 负责 sub-agent 执行，`TaskManager` 负责后台任务生命周期。

## 方案选择

采用方案 A：小范围结构修复加针对性回归测试。

没有采用只禁用并发的止血方案，因为它不能真正修复 per-call event queue 的共享状态问题，也会让 agent tool 调用失去本可安全并行的能力。没有采用完整 turn 模型重构，因为当前问题可以在现有 message 和 task 抽象内清晰解决，重构会牵动 session persistence、renderer 和 provider 适配层，风险高于收益。

## #82 Context Compaction 设计

`ContextManager._split_messages_for_compaction()` 继续返回 `(old_messages, recent_messages)`，但不再直接用纯索引切分。流程如下：

1. 根据 `preserve_recent_turns` 计算初始切分点，保留当前行为的目标大小。
2. 检查切分点是否落在 tool round-trip 中间。
3. 如 recent 区域开头包含孤立的 `tool_result`，向前扩展 recent，直到匹配的 assistant `tool_use` 也在 recent 中。
4. 如 old 区域结尾包含尚未在 old 区域内配对的 `tool_use`，把该 assistant message 以及后续相关消息留在 recent 中。
5. 对纯文本消息保持现有行为：六轮 user/assistant 文本对话仍保留最后三轮。

安全边界定义为：本次 compaction 不能制造孤立的 `tool_result`，也不能把一个未完成的 `tool_use` 留在 summary 侧而把它的 result 留在 recent 侧。如果历史数据本身已经损坏，切分 helper 不负责修复全部历史，只保证不因本次切分产生新的不一致。

## #90 Event Queue 设计

`AgentLoop` 已经为每个带 progress 的 tool call 创建独立 `asyncio.Queue`。该 queue 应保持为一次调用的局部状态：

1. `ToolExecutor._validate_and_execute()` 不再检测或写入 tool 实例上的 `_event_queue`。
2. 如果 `ToolCallRequest.event_queue` 存在，`ToolExecutor` 创建新的 `ToolContext(cwd=context.cwd, event_queue=call.event_queue)` 并传入本次 `tool.execute()`。
3. `AgentTool.execute()` 从 `context.event_queue` 读取当前调用的 queue，传给 `run_sub_agent()`。
4. `AgentTool.execute()` 在成功或普通异常失败时只向当前 queue 写入 `None` sentinel，通知 `AgentLoop` 的 polling 收尾。

这样两个并发 agent calls 即使共享同一个 `AgentTool` 实例，也不会共享 progress queue。共享工具实例上不再有 per-call mutable queue 状态。

## #90 后台任务生命周期设计

后台 sub-agent 的 `asyncio.Task` 必须由 `TaskManager` 跟踪和取消：

1. `AgentTool.execute()` 在 `run_in_background` 为 true 时创建 background task。
2. 创建 task 后把 task handle 注册到 `TaskManager` 对应 task id。
3. `TaskManager.stop(task_id)` 如果发现任务仍在运行，则调用 `task.cancel()`，并把状态标为 `STOPPED`。
4. `AgentTool._run_background()` 显式捕获 `asyncio.CancelledError`，标记任务为 stopped 后重新抛出或正常返回，避免继续进入 complete/fail 路径。
5. 普通异常继续标记 `FAILED`，正常完成继续标记 `COMPLETED` 并记录 result、tool count 和 token count。

重复 stop 已经完成、失败或停止的 task 不抛错。完成或失败路径不应覆盖已经被用户停止的 task。

## 错误处理

#82 的 helper 在找不到匹配 `tool_use` 时选择保守保留更多 recent messages，而不是把孤立 `tool_result` 推给模型。若消息数量不足以 compaction，仍返回 `([], messages)`。

#90 的 event queue sentinel 在 `AgentTool.execute()` 的成功和错误路径都会写入。没有 `context.event_queue` 时保持当前同步执行行为，不产生 progress events。

#90 的后台任务取消路径使用 `asyncio.CancelledError` 专门处理，因为它不属于普通业务失败。取消后的用户可见状态是 `stopped`，不是 `failed`。

## 测试计划

新增或更新以下回归测试：

- `tests/services/test_context_manager.py`：构造包含 `ToolUseBlock` 和 `ToolResultBlock` 的历史，验证 compaction 后 recent messages 不以孤立 `tool_result` 开头，且不会拆散同一轮 tool round-trip。
- `tests/services/test_context_manager.py`：保留现有纯文本 compaction 行为，避免新算法改变普通 user/assistant 对话。
- `tests/tools/test_tool_executor.py` 或 `tests/agent/test_agent_tool.py`：构造两个并发 agent calls，各自带不同 queue，验证事件和 sentinel 进入各自 queue。
- `tests/agent/test_agent_tool.py`：验证 `AgentTool.execute()` 使用 `ToolContext.event_queue`，不再依赖实例 `_event_queue`。
- `tests/tasks/test_task_state.py`：验证 `TaskManager` 能保存 task handle，`stop()` 会取消未完成 task。
- `tests/agent/test_agent_tool.py`：验证 `_run_background()` 收到 cancellation 后标记 stopped，且不会随后 complete/fail。

实施时先跑相关测试；如果全量 `make test` 仍受当前 baseline 的 `src/iac_code/i18n/messages.pot` 缺失影响，需要在结果中明确标注该失败与本设计无关。

## 范围外

- 不重构完整 conversation turn 数据模型。
- 不改变 session persistence 文件格式。
- 不改变 UI renderer 的 progress event 展示协议。
- 不处理其他 open issues 中的 shell、安全、i18n 或 provider 问题。
