# Pipeline 回滚残留清理恢复说明

## 背景

selling pipeline 的 step5 在部署阿里云 ROS 资源栈后，如果中途被打断或回滚阶段异常退出，可能出现 `deployment.stack_id` 尚未写入最终 deployment 状态，但云上 ROS Stack 已经创建成功的情况。原有回滚逻辑只依赖当前内存状态，进程崩溃、REPL 恢复、A2A cancel 或异常 handoff 后都可能遗漏这些残留资源，导致云资源泄漏。

本次改动将 step5 rollback 中发现的待清理 ROS Stack 记录为可恢复的 cleanup ledger，并在 pipeline 最终进入 normal chat 后，通过正常 agent loop 自动发起一轮隐藏清理 prompt。用户仍然可以取消清理，但系统会把待清理资源、清理状态和进度持久化，便于恢复和观测。

## 主要改动

### Cleanup ledger

- 新增 `src/iac_code/pipeline/engine/cleanup.py`，负责记录 rollback cleanup resource、状态、进度、错误信息和 cleanup prompt。
- ledger 持久化到 session 目录，避免只保存在内存中，进程崩溃后仍能恢复。
- selling pipeline 的 deploying step hook 在 step5 rollback 相关路径中写入 cleanup resource，避免把 pipeline 框架和阿里云 ROS 细节硬编码绑定。

### Normal chat 自动清理

- pipeline 结束、异常、cancel 或恢复 handoff 到 normal chat 时，会检查 ledger 中仍需清理的资源。
- 发现残留后向 agent loop 注入一条 cleanup prompt，让现有工具调用和对话循环执行清理，不新增单独的清理执行器。
- cleanup prompt 会进入 session transcript，恢复会话后仍可追踪；REPL 渲染时隐藏 prompt 正文，只展示清理状态提示。

### REPL、A2A 和恢复

- REPL 恢复时会重放 cleanup ledger 的摘要，使用单行、带层级的清理事件展示，减少噪音。
- A2A pipeline snapshot、stream 和 executor 支持 cleanup state，恢复 session 后可以继续感知清理状态。
- cleanup 相关事件使用统一前缀和颜色语义区分等待、进行中、成功、失败等状态。
- 清理失败或未完成的资源在后续恢复 normal chat 时仍会被识别，用户继续会话后可再次触发清理。

### 可观测和工具事件

- ROS `DeleteStack`、`GetStack` 以及通用阿里云 API 调用会把 stack 事件传递给 cleanup 观察逻辑。
- cleanup 状态不只依赖 `DeleteStack` 提交成功，还会根据后续 stack 状态更新为 `DELETE_IN_PROGRESS`、`DELETE_COMPLETE`、`DELETE_FAILED` 等。
- A2A 和 REPL 都能从同一份 cleanup state 中读取进度，避免多 session 订阅互相干扰。

### `/prompt` 导出

- `/prompt` 在 normal chat 阶段不会误用已经结束的 pipeline prompt context。
- cleanup prompt 会在 `Cleanup Prompts` tab 中单独展示，便于诊断。
- 如果 cleanup prompt 已经从 provider messages 中被移除，只有在能基于 `session.jsonl` 找到前后稳定锚点时，才会插回 Provider Messages 并标记 `cleanup prompt · 已移除`。
- 如果找不到稳定锚点，则不会在 Provider Messages 中展示，避免误导真实发送顺序。

## 测试覆盖

新增和更新的测试覆盖了以下行为：

- step hook 能生成 cleanup resource，pipeline runner 会持久化 rollback cleanup ledger。
- normal chat handoff 会根据 ledger 注入 cleanup prompt。
- REPL 恢复能显示 cleanup 摘要和清理事件。
- A2A executor、pipeline snapshot、pipeline stream 和恢复逻辑能携带 cleanup state。
- ROS Stack 删除状态会通过 `GetStack` 后续检查更新为完成或失败。
- `/prompt` 能展示 cleanup prompt，并按稳定锚点决定是否插入 Provider Messages。
- A2A e2e recovery scenario 覆盖 cleanup 相关恢复路径。

## 国际化说明

本次 cherry-pick 没有出现 `messages.po` 冲突。新增和调整的用户可见字符串均使用项目现有 `_()` 调用方式，避免 f-string 包裹 `_()`。如果后续分支中翻译文件发生冲突，需要保留双方词条并执行 `make translate` 重新生成。
