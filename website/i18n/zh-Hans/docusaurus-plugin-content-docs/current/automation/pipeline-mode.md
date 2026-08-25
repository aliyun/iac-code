---
title: Pipeline 模式
description: 使用按步骤运行的 Pipeline 模式，引导完成复杂基础设施任务。
---

# Pipeline 模式

Pipeline 模式是一种按步骤执行任务的交互模式。它适合处理比普通聊天更长、更容易出错的基础设施工作：先理解需求，再规划方案、生成产物、让用户确认，最后继续执行后续动作。

Pipeline 本身是通用能力；当前内置实现的是 `selling` pipeline。`selling` 面向阿里云基础设施场景，可以帮助用户从一句部署需求出发，逐步得到候选架构、ROS 模板、成本估算，并在确认方案后继续部署。

适合使用 Pipeline 模式的请求包括：

```text
选择一个已有 VPC，创建一个 VSwitch
```

```text
帮我设计一个低成本的阿里云 Web 应用部署方案，并生成模板
```

## 启动 Pipeline 模式

Pipeline 模式可以通过交互式 REPL 或 SDK process 模式运行，不能和 `--prompt` 一起使用。

在 macOS 或 Linux 上：

```bash
IAC_CODE_MODE=pipeline iac-code
```

在 PowerShell 上：

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

默认 pipeline 名称是 `selling`。如需显式指定：

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

对于 SDK 子进程客户端，可以用 stream-json 输入和输出启动 process 模式：

```bash
IAC_CODE_MODE=pipeline iac-code --input-format stream-json --output-format stream-json
```

## Pipeline 与 selling 的关系

| 名称 | 含义 |
|---|---|
| Pipeline 模式 | IaC Code 的通用分步执行模式，用来承载长流程、确认点、恢复和进度展示。 |
| `selling` pipeline | 当前内置的 pipeline，用于阿里云基础设施方案设计、模板生成、成本估算和部署。 |

后续如果提供更多 pipeline，可以通过 `IAC_CODE_PIPELINE_NAME` 选择。当前发布版本内置的是 `selling`。

## 环境变量

| 变量 | 用途 |
|---|---|
| `IAC_CODE_MODE=pipeline` | 启用 Pipeline 模式。其他值会回退到普通模式。 |
| `IAC_CODE_PIPELINE_NAME` | 选择 pipeline 定义，默认值为 `selling`。 |
| `IAC_CODE_CWD` | 覆盖 pipeline 使用的工作目录。 |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | 启用 `selling` pipeline 中可选的模板审查步骤。 |

## 使用 selling pipeline 时会发生什么

`selling` pipeline 会把一次基础设施需求拆成几个用户可理解的阶段：

| 阶段 | 用户会看到什么 |
|---|---|
| 理解需求 | IaC Code 会判断这是不是阿里云基础设施需求。信息不够时，会先提问，而不是直接生成方案。 |
| 规划方案 | IaC Code 会给出一个或多个候选架构，方便你比较不同取舍。 |
| 生成与评估 | IaC Code 会为候选方案生成 ROS 模板，并估算相关资源成本。 |
| 确认方案 | IaC Code 会展示候选方案详情，等待你选择要继续的方案。 |
| 执行部署 | 选择方案后，IaC Code 会进入部署阶段，并在需要执行工具或高风险操作时按权限策略处理。 |

如果你明确提到“已有 VPC”“不要创建某类资源”这类约束，`selling` pipeline 会在后续方案和模板中尽量遵守这些要求。用户不需要了解内部字段，只需要把约束写在需求里。

## 交互与恢复

Pipeline 运行时可能会暂停等待用户输入，例如：

- 需求不清楚，需要你补充目标、规模、地域或预算。
- 有多个候选方案，需要你选择一个继续。
- 需要执行工具或部署操作，必须先经过权限确认。
- 运行过程中被中断，需要恢复或继续。

如果进程退出或会话中断，IaC Code 会保存 pipeline 状态。之后使用 `--resume` 回到该会话时，可以继续查看之前的进度，并从可恢复的位置继续。

当 pipeline 完成、失败、提前退出或被取消后，IaC Code 会切回普通聊天。这样你可以继续追问、修改方案，或处理部署后的问题。

切回普通聊天还要求 pipeline 已产出必需结果。如果某个必需步骤还没有产出可用结果——例如在需求理解阶段仍在执行时就取消了本次运行——IaC Code 只会发布终态，不会发出移交就绪信号，避免下游把空结果当成 pipeline 已经完成工作。

## 自动化集成

Pipeline 模式可以通过 A2A 服务模式或 SDK process 模式集成。A2A 服务模式会对外暴露 pipeline 进度、产物、权限结果和恢复信息，适合把 pipeline 接入外部控制台或任务系统。SDK process 模式会把 `iac-code` 保持为本地子进程，并通过 stdin/stdout 交换按行分隔的 JSON。

通过 A2A 集成 pipeline 时，调用方还可以在消息元数据中声明两项能力：传 `metadata.iac_code.preferredLanguage` 让进度、提问和权限提示按调用方期望的语言返回；传 `metadata.iac_code.candidatePresentation: rich-v1` 让方案确认步骤返回结构化载荷（候选名称、摘要、架构图、月度总成本、分项成本），适合在外部界面做富渲染。当部署或工具操作需要权限时，pipeline 会发布待决权限信封，调用方通过旁路消息应答 `allow_once` 或 `deny`，协议细节见[协议参考](../a2a/protocol-reference.md)。

在 SDK process 模式中，pipeline 事件会以 `stream_event` frame 发出，事件 payload 的 `type` 为 `"pipeline_event"`。最终 `result` frame 会包含 `pipeline` 对象，其中有 `contextId`、`taskId`、`iacCodeSessionId`、`status` 和 `sidecarStatus`。暂停中的 pipeline 应使用相同的 `contextId` 和活跃 `taskId` 恢复。如果某个 context 存在可恢复 task 但客户端省略 task id，process 会返回可重试的 `pipeline_task_required` 错误，并携带 `recoverableTaskId`。

ACP 目前不支持 Pipeline 模式。`--prompt` / [非交互模式](./non-interactive-mode.md) 会走普通一次性调用，不会执行 Pipeline 步骤。

## 当前限制

- 当前内置 pipeline 只有 `selling`，主要面向阿里云基础设施工作流。
- Pipeline 模式支持交互式 REPL 和 SDK process 模式；当 `IAC_CODE_MODE=pipeline` 时，`--prompt` 会被拒绝。
- Pipeline 模式支持文本输入。Pipeline 激活时，粘贴到 REPL 的图片会被忽略。
- Pipeline 运行期间，shell escape、技能触发器和大多数 slash command 会被限制，除非 pipeline 定义显式允许。`/help`、`/status`、`/resume`、`/exit` 等基础命令仍然可用。


## 备份检查点

Pipeline 模式不再为完成的 agent loop step 执行备份。它会先发布 `input_required` 或 `waiting_input`，再执行一次关键备份；如果备份失败，pipeline 随后发送 `backup_blocked` 并停在可恢复状态。`pipeline_handoff_ready` 和终态仍然会在发布前受到关键备份保护。对 A2A 观察者来说，当前模式的备份边界持久化后，terminal 和 `pipeline_handoff_ready` 受保护发布会跟随一个包含 `committedEventId`、`committedEventType` 和 `committedSequence` 的 `backup_committed` 事件。配置 `IAC_CODE_CONFIG_BACKUP_TMP_DIR` 时，该边界是本地不可变快照，可用于同一 sandbox 内恢复；跨 sandbox 恢复要等后台进程将其复制到 `IAC_CODE_CONFIG_BACKUP_DIR`。未启用临时目录时，该边界仍是最终备份目录。`parallel_sub_pipeline` 的子 step 进度由下一次等待输入、handoff 或终态备份捕获，不再创建逐步检查点。
