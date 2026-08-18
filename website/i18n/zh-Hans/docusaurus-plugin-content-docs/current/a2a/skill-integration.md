---
sidebar_position: 7
title: Skill 集成
description: 外部 agent 通过打包的 iac-code Skill 与 Skill Runtime 驱动 iac-code。
---

# Skill 集成

iac-code 随仓库提供一个面向外部 agent 的 Skill 包。外部 agent（上层规划 agent、agent 平台）不需要安装 iac-code 的 Python 包，也不直接调用 headless 命令，而是通过一个纯标准库的桥接脚本驱动本地认证的 A2A runtime，完成 ROS/Terraform 模板生成、成本估算、资源选择与部署等阿里云基础设施任务。

## 组成

| 组件 | 位置 | 说明 |
|---|---|---|
| Skill 包 | `skills/iac-code/` | `SKILL.md` 使用说明、`agents/` agent 元数据、`scripts/iac_code.py` 桥接脚本 |
| Skill Runtime | 按平台发布 | 内置 iac-code A2A server 的 CPython 3.12 原生可执行文件 |
| 分发契约 | `skill-runtime/skill-package-contract.json`、`skill-runtime/publisher-contract.json` | Skill 包与发布者的格式和校验约束 |

桥接脚本完全使用 Python 标准库编写并兼容 Python 3.8+，CI 在 3.8–3.14 全矩阵上编译并冒烟运行它。不要为桥接脚本引入第三方依赖或仅新版本支持的语法。

## Runtime 获取与缓存

桥接脚本在首次执行时读取 manifest、下载对应平台的 Runtime 产物，校验大小和 SHA-256 后安装，并缓存在 `<IAC_CODE_CONFIG_DIR 或 ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/` 下。

- `python3 scripts/iac_code.py ensure-runtime` — 提前完成 Runtime 准备；已缓存时直接复用。
- `python3 scripts/iac_code.py cache list` — 查看已安装的 Runtime 与候选包。
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — 清理 Runtime 缓存或候选包；必须显式传 `--confirm`。

## 配置预检

`start` 在创建任务前会通过 Runtime 做一次不读取密钥明文的配置就绪检查：

| 情况 | 结果 |
|---|---|
| LLM provider 或 API Key 不完整 | 返回 `llm_not_configured`，拒绝创建任务 |
| selling Pipeline 且阿里云凭证不完整 | 返回 `cloud_credentials_not_configured`，拒绝创建任务 |
| normal 模式且阿里云凭证不完整 | 可继续执行不调用云 API 的任务，但会给出预检警告 |

## 命令参考

| 命令 | 用途 |
|---|---|
| `start` | 创建任务：`--mode normal|pipeline`、`--pipeline-name`、`--cwd` 绝对工作区、`--prompt-file` UTF-8 提示文件、`--language auto|en|zh|es|fr|de|ja|pt`，可选 `--follow` |
| `follow` | 消费事件流直到下一个交互边界：`--job-id`、`--cursor`、`--wait-seconds`（默认 60 秒，最大 120 秒） |
| `continue` | 在同一 job 中继续 normal 模式对话：`--job-id`、`--prompt-file`，可选 `--follow` |
| `respond` | 应答一次待决输入，见[用户输入](#input-required) |
| `poll` | 仅用于诊断和恢复的单次轮询，不要用它替代 `follow` |
| `cancel` | 取消 job |
| `ensure-runtime` / `cache list` / `cache clean` | Runtime 与缓存管理 |

`start --follow` 与 `follow` 会把步骤边界和低频心跳写到 stderr，stdout 只输出一条有界 JSON 结果。

## 交互边界

`--follow` 会消费事件流，直到遇到下一个步骤边界、权限请求、用户提问、候选选择、`turn_completed` 或终态。到达边界时，结果会携带：

- `boundaryReached: true` — 表示到达边界，**不代表任务完成**；
- `presentationRequired: true` 与 `userUpdates` — 已本地化、可直接展示给用户的文本；
- 继续执行所需的 `cursor`。

外部 agent 必须先把它收到的 `userUpdates` 逐条呈现在用户可见的回复正文中，再立即用返回的 `cursor` 继续 `follow`；在 follow 运行期间不要并行回答基础设施问题或提出无关提问。

## 用户输入（inputRequired） {#input-required}

结果中出现 `inputRequired` 时表示需要用户输入，共三类：

- `permission` — 工具或部署权限请求。信封包含 `inputId`、`toolUseId`、标题、目的、影响、目标、是否只读、`safeSummary`，部署类请求还包含 `deploymentSummary`。外部 agent 应按自身权限策略决策；同等操作若在其直接执行时无需询问，可应答 `allow_once`，策略不允许则应答 `deny`，其余情况询问用户。iac-code 自身的拒绝决策不可被覆盖。
- `ask_user_question` — 选择题或自由文本提问。原样呈现提示与选项；仅当 `allowFreeText` 为 `true` 时才接受自由文本。
- `candidate_selection` — Pipeline 方案选择。先呈现每个候选的摘要、架构图（Mermaid）、月度总成本与分项成本，再返回所选候选，不要用粗略估价替换给出的价格。

`respond` 有两种形式：

```bash
# 权限的内联决策
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# 问题与候选选择使用应答文件
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

应答必须完整保留待决输入的所有关联字段，且只绑定当前的 `kind`、`inputId`、`requestTaskId` 与 `contextId`；不得复用其他请求的应答，也不得把资源选择的应答重新解释为部署确认。

## 语言控制

`start --language` 指定 job 的首选语言（未知时用 `auto`）。此后该 job 的每次结果都会重复 `preferredLanguage`，应把它当作持久控制状态：进度、提问、权限提示、候选方案与最终结果都按该语言呈现；协议字段名、枚举值、ID 和命令保持不变。当权威文本已经使用该语言时，直接呈现或同语言摘要，不要把中文用户可见内容翻译成英文。

## 与 A2A 协议的关系

桥接脚本通过 HTTP A2A JSON-RPC 与本地 Runtime 通信，任务状态、artifact 与权限交互都复用 iac-code 的 A2A 协议：

- 权限旁路应答使用 `schemaVersion 1` 的消息格式，字段与约束见[协议参考](./protocol-reference.md)。
- Pipeline 模式下传入 `candidatePresentation: rich-v1` 可获得结构化候选展示载荷。
- job 结果状态与 A2A 任务状态对应：`turn_completed` 表示 normal 轮次完成；Pipeline 终态包括 `completed`、`failed`、`canceled`、`rejected`，到达终态时以 `pipelineResult` 与 `artifacts` 为权威结果。

## 安全边界

- Runtime 只监听 `127.0.0.1` 上的随机端口，每次启动生成独立的随机 Bearer token，桥接脚本的每个请求都携带该 token。
- 桥接脚本把产物和结果限制在 job 工作区内，结果写入工作区的 `.iac-code-skill-results/`。
- 预检与权限展示字段均经脱敏处理；密钥、凭证等敏感值不会出现在展示字段中。
