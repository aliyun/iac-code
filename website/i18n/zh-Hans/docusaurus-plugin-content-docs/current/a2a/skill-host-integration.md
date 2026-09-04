---
sidebar_position: 3
title: IaC Code Skill 宿主集成参考
description: 在支持 Skill 的宿主 Agent 中集成 IaC Code 桥接脚本。
---

# IaC Code Skill 宿主集成参考

本文面向 Agent 和 Skill 分发系统的开发者，说明宿主如何调用桥接脚本、展示 IaC Code 结果、处理用户交互
并恢复已有任务。普通用户请阅读[安装和使用 IaC Code Skill](./skill-integration.md)。

## 集成模型

Skill 包含 `SKILL.md` 和只使用 Python 标准库的 `scripts/iac_code.py`。宿主调用桥接脚本，桥接脚本安装并
启动固定版本且经过校验的 Runtime，然后通过本地鉴权 A2A 连接与其通信。

宿主必须：

- 使用 CPython 3.8～3.14 运行桥接脚本；
- 将 stdout 视为稳定的 JSON 结果，将 stderr 视为诊断和受限的进度输出；
- 保存当前 `jobId`、`contextId`、cursor 和输入关联字段；
- 展示每个面向用户的边界后再继续；
- 桥接出错时终止流程，不得改用云 API 直调或其他 Runtime 绕过。

## 可选分发配置

分发方可以在 `SKILL.md` 同级目录放置 `config.json`：

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

- `channel` 是渠道标识，桥接脚本会自动添加 `skill/` 前缀。
- `pipelineName` 仅在选择 Pipeline 模式后生效。默认值为 `selling_solution_first`；仅当分发方明确需要旧流程
  时才使用 `selling`。
- `permissionWaitPolicy` 控制 Skill 临时 A2A Server 的等待策略。常驻或子 Pipeline 超时为 `null` 表示无限等待。

桥接脚本会拒绝未知字段和非法值。此文件属于安装策略，不得根据用户请求生成、在任务输出中展示或在任务
执行期间修改。

## 启动作业

把完整请求写入工作目录中的 UTF-8 文件，将工作目录解析为绝对路径，然后运行：

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

默认使用 `normal`。仅在用户需要候选架构、费用对比、确认和部署组成的方案对比流程时选择 `pipeline`。
语言可以设置为 `en`、`zh`、`es`、`fr`、`de`、`ja`、`pt` 或 `auto`。后续轮次始终保留返回的
`preferredLanguage`。

`start` 会进行不读取密钥值的就绪检查。`llm_not_configured` 会在创建作业前终止。Pipeline 模式还要求
云凭证完整，否则返回 `cloud_credentials_not_configured`。普通模式在任务不需要云 API 时可以带警告继续。

## 跟进进度和完成状态

`--follow` 会在下一个展示或交互边界、`turn_completed` 或 Pipeline 终态停止。结果包含
`boundaryReached: true` 时，先展示 `userUpdates` 中的全部文本，再使用返回的 cursor 继续同一作业：

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

不要把 `boundaryReached` 当作任务完成。`presentationRequired` 表示继续调用桥接脚本前必须让用户看到当前
更新。普通模式只有 `state` 为 `turn_completed` 时结果才具有权威性，应使用 `finalText` 和 `artifacts`。
Pipeline 到达终态后使用 `pipelineResult` 和 `artifacts`，清理失败时必须明确告知，不能宣称任务成功。

诊断或恢复期间无法使用 `follow` 时，可以轮询同一作业：

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

如果结果显示 `state: input-required`，但没有 `inputRequired`，请报告最新文本或错误并保持作业不变，不要
重复提交回答或新建替代作业。

## 处理用户输入

每个 `inputRequired` 都是必须暂停的交互边界。通过宿主原生的提问或审批界面展示它，等待用户明确回答。
不得从原始请求推断答案或替用户选择默认项。必须保留 `kind`、`inputId`、`requestTaskId`、`contextId`，
以及存在时的 `toolUseId`。

| `kind` | 宿主需要展示 | 回答 |
|---|---|---|
| `permission` | 用途、影响、目标、是否只读、部署摘要、安全摘要和可选操作 | `allow_once` 或 `deny` |
| `ask_user_question` | 问题、选项，以及允许自由输入时的提示 | 选项或允许的自由文本 |
| `candidate_selection` | 每个方案摘要、Mermaid 架构图、月费用总计和费用项 | 方案 ID 或序号 |
| `deployment_confirmation` | 方案、模板地址、报价或报价失败、有效参数、参数覆盖、预览状态和可选操作 | `confirm`、`adjust`、`reselect` 或 `cancel` |

将带有关联字段的回答写入新的 UTF-8 JSON 文件，并恢复同一作业：

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

回答示例：

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

用户未要求调整时省略 `parameterOverrides`。用户提出部署需求，不代表已经同意后续的
`deployment_confirmation`；宿主自身的审批也不能覆盖 IaC Code 的拒绝结果。

## 继续对话

普通模式一轮完成后，或 Pipeline 完成并将对话切换到普通模式后，把下一条消息写入新的提示词文件并继续
已有作业：

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

保持相同的 `jobId` 和 `contextId`；普通模式每轮产生新的 `taskId` 属于正常行为。不得仅因上一轮结束就改用
`start`。保留作业标识还能让桥接脚本恢复权限等待，并在宿主中断后继续执行。

取消整个操作时运行：

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

取消整个操作与拒绝单次权限请求不同。

## 错误和 Runtime 生命周期

创建作业前返回的桥接错误是权威结果。特别是 `incompatible_host` 会返回可用的宿主和 Runtime 兼容性信息；
展示这些信息后停止。不得改用 pip 安装、其他 Runtime 软件包或云 API 直调。

Runtime 缓存在 `<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`。软件包布局和
完整性元数据由 `skill-runtime/skill-package-contract.json` 及版本清单定义，使用前由桥接脚本校验。
清理 Runtime 缓存必须是用户单独明确要求的操作，当前和正在使用的软件包会受到保护。

Runtime 绑定随机的 `127.0.0.1` 端口，并生成进程专用的 Bearer Token。不得暴露 Token、本地状态、凭证、
环境变量值或原始工具输入输出。受限的结果投影和展示字段才是宿主支持的接口。

## 相关文档

- [IaC Code 官方 Skills 概览](./skill-overview.md)
- [安装和使用 IaC Code Skill](./skill-integration.md)
- [A2A 协议概览](./overview.md)
- [A2A 协议参考](./protocol-reference.md)
- [运行时配置](../configuration/runtime-configuration.md)
