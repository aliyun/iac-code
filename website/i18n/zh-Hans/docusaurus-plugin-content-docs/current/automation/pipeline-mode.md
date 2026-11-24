---
title: Pipeline 模式
description: 使用按步骤运行的 Pipeline 模式，引导完成复杂基础设施任务。
---

# Pipeline 模式

Pipeline 模式是一种按步骤执行任务的交互模式。它适合处理比普通聊天更长、更容易出错的基础设施工作：先理解需求，再规划方案、生成产物、让用户确认，最后继续执行后续动作。

Pipeline 本身是通用能力。IaC Code 内置两条面向阿里云购买场景的流水线：默认的 `selling` 和需要显式选择的 `selling_solution_first`。两者都覆盖架构规划、ROS 模板、成本估算、确认和部署，但实现候选方案的顺序不同。

适合使用 Pipeline 模式的请求包括：

```text
选择一个已有 VPC，创建一个 VSwitch
```

```text
帮我设计一个低成本的阿里云 Web 应用部署方案，并生成模板
```

## 启动 Pipeline 模式

Pipeline 模式支持 Web、桌面版、A2A 和 SDK process 模式。单次 `--prompt` 模式不支持流水线。

正常启动 Web 应用（或直接打开桌面版）：

```bash
iac-code
```

新建会话时，在界面选择**普通模式**或**流水线模式**。流水线模式下可选择**售卖流水线**（`selling`）或**售卖流水线（先选方案）**（`selling_solution_first`）。同一个应用中的不同会话可以使用不同模式和流水线，无需设置环境变量或重启服务。

对于 SDK 子进程客户端，可以通过环境变量选择默认的 Pipeline 模式，并使用 stream-json 输入和输出：

```bash
IAC_CODE_MODE=pipeline iac-code --input-format stream-json --output-format stream-json
```

## 可用的流水线

| 名称 | 含义 |
|---|---|
| Pipeline 模式 | IaC Code 的通用分步执行模式，用来承载长流程、确认点、恢复和进度展示。 |
| `selling` pipeline | 先生成并评估候选模板，再由用户选择一个方案部署；它仍是默认值。 |
| `selling_solution_first` pipeline | 先由用户选择架构，再只为该方案生成模板、执行预览和询价，最后部署。 |

Web 和桌面版在新建会话时选择具体流水线；A2A 和 SDK process 模式可通过 `IAC_CODE_PIPELINE_NAME` 设置默认流水线。三阶段流程、独立的部署确认与权限边界以及恢复行为，请参阅[方案优先流水线](./solution-first-pipeline.md)。

## 环境变量

这些变量设置 A2A 和 SDK process 模式的默认值。Web 和桌面版按会话选择模式与流水线；Web API 仅在请求省略对应字段时，回退到 `IAC_CODE_MODE` 和 `IAC_CODE_PIPELINE_NAME`。

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

如果进程退出或会话中断，IaC Code 会保存流水线状态。在 Web 或桌面版的侧栏重新打开该会话，即可查看已有进度，并从可恢复的位置继续。

当 pipeline 完成、失败、提前退出或被取消后，IaC Code 会切回普通聊天。这样你可以继续追问、修改方案，或处理部署后的问题。

## 自动化集成

Pipeline 模式可以通过 A2A 服务模式或 SDK process 模式集成。A2A 服务模式会对外暴露 pipeline 进度、产物、权限结果和恢复信息，适合把 pipeline 接入外部控制台或任务系统。SDK process 模式会把 `iac-code` 保持为本地子进程，并通过 stdin/stdout 交换按行分隔的 JSON。

通过 A2A 集成 pipeline 时，调用方还可以在消息元数据中声明两项能力：传 `metadata.iac_code.preferredLanguage` 让进度、提问和权限提示按调用方期望的语言返回；传 `metadata.iac_code.candidatePresentation: rich-v1` 让方案确认步骤返回结构化载荷（候选名称、摘要、架构图、月度总成本、分项成本），适合在外部界面做富渲染。当部署或工具操作需要权限时，pipeline 会发布待决权限信封，调用方通过旁路消息应答 `allow_once` 或 `deny`，协议细节见[协议参考](../a2a/protocol-reference.md)。

在 SDK process 模式中，pipeline 事件会以 `stream_event` frame 发出，事件 payload 的 `type` 为 `"pipeline_event"`。最终 `result` frame 会包含 `pipeline` 对象，其中有 `contextId`、`taskId`、`iacCodeSessionId`、`status` 和 `sidecarStatus`。暂停中的 pipeline 应使用相同的 `contextId` 和活跃 `taskId` 恢复。如果某个 context 存在可恢复 task 但客户端省略 task id，process 会返回可重试的 `pipeline_task_required` 错误，并携带 `recoverableTaskId`。

ACP 目前不支持 Pipeline 模式。`--prompt` / [非交互模式](./non-interactive-mode.md) 会走普通一次性调用，不会执行 Pipeline 步骤。

## 当前限制

- 当前内置 `selling` 和 `selling_solution_first`，两者主要面向阿里云基础设施工作流；`selling` 仍是默认值。


## 备份检查点

Pipeline 模式不再为完成的 agent loop step 执行备份。它会先发布 `input_required` 或 `waiting_input`，再执行一次关键备份；如果备份失败，pipeline 随后发送 `backup_blocked` 并停在可恢复状态。`pipeline_handoff_ready` 和终态仍然会在发布前受到关键备份保护。对 A2A 观察者来说，当前模式的备份边界持久化后，terminal 和 `pipeline_handoff_ready` 受保护发布会跟随一个包含 `committedEventId`、`committedEventType` 和 `committedSequence` 的 `backup_committed` 事件。配置 `IAC_CODE_CONFIG_BACKUP_TMP_DIR` 时，该边界是本地不可变快照，可用于同一 sandbox 内恢复；跨 sandbox 恢复要等后台进程将其复制到 `IAC_CODE_CONFIG_BACKUP_DIR`。未启用临时目录时，该边界仍是最终备份目录。`parallel_sub_pipeline` 的子 step 进度由下一次等待输入、handoff 或终态备份捕获，不再创建逐步检查点。
