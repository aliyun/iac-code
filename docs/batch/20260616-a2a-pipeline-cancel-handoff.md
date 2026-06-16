# A2A Pipeline Cancel Handoff 修复说明

## 背景

本次修复围绕 A2A HTTP 服务、pipeline 执行器和 `scripts/a2a/debugger.py` 展开。问题来自两类场景：

- A2A executor 在早期异常路径中先发送 `TaskStatusUpdateEvent`，但 A2A SDK 要求 agent 先 enqueue `Task`，导致真实错误被包装成 `Agent should enqueue Task before TaskStatusUpdateEvent event`。
- selling pipeline 在等待用户选择时执行 cancel 后，预期应发布 `pipeline_canceled` 和 `pipeline_handoff_ready`，并进入 normal chat handoff；debugger 也需要能拉取和展示这些事件。

## 主要改动

### A2A executor

- 在 `src/iac_code/a2a/executor.py` 中增加初始 Task 入队保护。
- `execute()` 内部现在会在发送任何状态更新前确保已经 enqueue 初始 Task。
- 即使 workspace metadata 校验失败等早期异常发生，也会先满足 A2A SDK 的事件顺序要求，然后再返回真实失败原因。

### Pipeline cancel handoff

- 在 `src/iac_code/a2a/pipeline_executor.py` 中补齐等待输入阶段的 cancel 处理。
- `cancel_waiting_input_task_from_sidecar()` 现在会写入 `pipeline_canceled` 事件。
- 当 pipeline 配置允许 `canceled` 状态执行 `switch_to_normal` 时，会继续生成 `pipeline_handoff_ready` 事件。
- handoff summary 会优先使用 sidecar pipeline session 的上下文快照，必要时回退到 A2A snapshot 中的步骤结论。
- cancel 和 handoff 事件的 sequence 会基于当前 high-water mark 递增，保证 debugger 可以从 `afterSequence` 正确拉取增量事件。

### A2A debugger

- 在 `scripts/a2a/debugger.py` 中，cancel 请求完成后会主动调用 `/api/pipeline/state` 拉取最新 pipeline 状态。
- 识别 `pipeline_canceled` 并在 timeline 中展示。
- pipeline task 进入 `canceled`、`failed`、`completed` 等终态后，后续 Stream 不再复用旧 pipeline task id，避免 normal chat 被错误绑定到已结束 task。
- 修复 debug log replay/export 对 pipeline state response 的解析：
  - 支持直接包含 `eventType` / `event_type` 的 pipeline event。
  - 支持从 `snapshots.jsonl` response 的 `events` 字段恢复 timeline。
  - 支持解析带 `{ "snapshot": ... }` 包装的 snapshot response。

### Debugger 文档

- 在 `scripts/a2a/debugger.md` 中补充 `--default-cwd` 的含义。
- `--default-cwd` 会作为 A2A workspace metadata 传给 server，表示 agent 执行任务时使用的 workspace，不是 debugger 自身的启动目录。
- 如果该目录不存在、不是目录，或者服务端策略不允许，会返回 `Invalid A2A workspace metadata.`。

## 测试覆盖

新增和更新的测试覆盖了以下行为：

- A2A streaming 先返回初始 Task，再返回状态事件。
- workspace metadata 错误会以任务失败形式返回，而不是触发 SDK 协议错误。
- 等待输入阶段 cancel 后会产生 `pipeline_canceled` 和 `pipeline_handoff_ready`。
- debugger cancel 后会主动拉取 pipeline state。
- debugger 不会在 pipeline 终态后继续复用旧 task id。
- debugger timeline 能展示 `pipeline_canceled`。
- debug log replay 能恢复 cancel/handoff 事件和 normal handoff summary。

## 国际化说明

本次 cherry-pick 没有修改 `messages.po` 或生成的翻译文件，也没有遇到国际化冲突，因此不需要额外合并翻译词条。若后续修改新增可翻译字符串，应按项目流程执行 `make translate`。
