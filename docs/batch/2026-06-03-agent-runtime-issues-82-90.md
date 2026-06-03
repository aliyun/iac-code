# Agent runtime issues #82/#90 修复说明

## 背景

本批次处理 GitHub issue #82 和 #90，范围集中在 agent runtime 的上下文压缩、后台任务生命周期以及并发工具调用的进度事件隔离。

- #82：`ContextManager` 使用 `preserve_recent_turns * 2` 按消息数量切分上下文，可能把 assistant 的 `tool_use` 和后续 user 的 `tool_result` 分到压缩边界两侧，形成孤立工具结果。
- #90：`AgentTool` 后台任务通过 `asyncio.create_task()` fire-and-forget 启动，`TaskManager.stop()` 只能改状态，不能取消实际协程；同时 `ToolExecutor` 把 per-call `event_queue` 写到共享 tool 实例，多个并发 `AgentTool` 调用会互相覆盖队列。

## 主要改动

### 上下文压缩边界

- 在 `src/iac_code/services/context_manager.py` 中新增 tool-use/tool-result ID 扫描逻辑。
- 初始 split 点仍保留原有 `preserve_recent_turns` 语义，但如果 old messages 中存在未配对的 `tool_use`，会把 split 点回退到对应 `tool_use` 之前。
- 这样压缩后的 summary 后面不会跟随孤立 `tool_result`，也不会把未完成的 `tool_use` 留在 summary 侧。

### 后台 agent 任务生命周期

- 在 `TaskInfo` 中保存 `background_task` 句柄。
- `AgentTool.execute()` 启动后台 agent 后会把 `asyncio.Task` 注册到 `TaskManager`，并添加 done callback 消费异常，避免未取回异常告警。
- `TaskManager.stop()` 现在只对 running task 生效，并会 cancel 已注册的 asyncio task。
- `_run_background()` 捕获 `asyncio.CancelledError` 后把任务标记为 stopped，并保留 stopped 状态，避免后续 complete/fail 覆盖用户停止结果。

### 进度事件隔离

- `ToolExecutor` 不再向共享 tool 实例写 `_event_queue`。
- per-call `event_queue` 通过 `ToolContext` 传给工具执行过程。
- `AgentTool` 只读取 `context.event_queue`，因此即使 `AgentTool.is_concurrency_safe()` 仍为 `True`，并发调用也不会共享队列状态。

### 用户可见行为

- `/tasks stop <id>` 和 `task_stop` 对已经 completed/failed/stopped 的任务不再误报 stopped，而是返回当前状态。
- 后台 agent 启动路径会关闭当前调用的 event queue sentinel，避免事件消费者等待不到结束信号。

## 兼容性判断

- `ToolContext` 已经包含 `event_queue` 字段，本次只是改为使用既有上下文字段传递 per-call 状态，没有改变 tool 输入 schema。
- `TaskManager.stop()` 返回值从无返回改为 `bool`；仓库内调用点已同步更新。返回值只增强状态表达，不影响忽略返回值的旧调用方式。
- `AgentTool.is_concurrency_safe()` 保持 `True`，因为共享可变 `_event_queue` 状态已经移除，不需要牺牲并发能力。
- 本次没有新增依赖，没有修改用户配置格式，也没有引入真实云账号或 LLM 调用依赖。
- cherry-pick 过程没有发生冲突，未触发 `message.po` 合并；本批次未新增用户可翻译字符串的 gettext 调用，因此未运行 `make translate`。

## 验证

已在隔离 worktree 和合入目标分支上执行重点回归验证：

```bash
PATH=/Users/ehzyo/.local/bin:$PATH uv run pytest \
  tests/services/test_context_manager.py \
  tests/tools/test_tool_executor.py \
  tests/agent/test_agent_tool.py \
  tests/tasks/test_task_state.py \
  tests/tasks/test_task_tools.py \
  tests/commands/test_tasks.py \
  -v
```

重点验证项包括：

- compaction 不切开 `tool_use` / `tool_result` round trip。
- 未完成 `tool_use` 会留在 recent messages。
- event queue 只通过 `ToolContext` 传递，不写入共享 tool 实例。
- 后台 agent task 会被 attach 到 `TaskManager`。
- `/tasks stop` / `task_stop` 会取消底层 asyncio task。
- stopped 状态不会被后台完成或失败覆盖。
- 已完成任务再次 stop 时返回 already completed。

额外运行：

```bash
PATH=/Users/ehzyo/.local/bin:$PATH make lint
PATH=/Users/ehzyo/.local/bin:$PATH make test
```

`make lint` 通过。全量 `make test` 的已知基线问题是 `tests/test_i18n.py` 中 4 个失败，原因是 `src/iac_code/i18n/messages.pot` 缺失；该问题在本批次修改前已存在，和 #82/#90 修复无关。

## 交互式验证说明

这两个问题不适合只靠启动 `iac-code` 手工验证：

- #82 需要上下文接近压缩阈值，并且 split 点刚好落在工具调用 round trip 中间，人工复现不稳定。
- #90 的 queue cross-talk 需要同一轮并发 agent tool 调用，依赖模型调度，手工观察很难稳定复现。

因此本批次主要通过自动化测试锁定原始失败模式。
