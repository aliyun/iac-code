---
title: 配置
description: 运行时配置顺序和本地文件。
---

# 配置

IaC Code 会从 CLI 参数、环境变量以及运行时配置目录中的文件读取配置。

配置优先级：

```text
CLI 参数 > 环境变量 > 配置文件
```

运行时目录默认为：

```text
~/.iac-code/
```

可通过设置 `IAC_CODE_CONFIG_DIR` 环境变量更改该目录（支持 `~` 和 `$VAR` 展开）。设置后，所有持久化产物——凭证、设置、历史、`projects/`、`image-cache/`、`tool-results/`、`logs/`、`memory/`、`a2a/`、`telemetry/`、`skills/`——都会跟随到新位置。启动/调试日志默认位于 `<config-dir>/logs/`，可用 `IAC_CODE_LOG_DIR` 单独移动；权限审计记录仍位于 `<config-dir>/logs/`。

常见文件：

| 文件 | 说明 |
|---|---|
| `.credentials.yml` | LLM 凭证 |
| `.cloud-credentials.yml` | 云厂商凭证 |
| `settings.yml` | 已选择的提供商、模型和相关设置 |
| `AGENTS.md` | 作为持久指令加载的用户记忆 |
| history files | 交互式工作流的输入历史 |

避免提交或分享该目录中的文件，因为它们可能包含密钥或本地偏好。

## 记忆文件

IaC Code 有两个公开的记忆位置：

| 位置 | 用途 |
|---|---|
| `<project-root>/AGENTS.md` | 项目记忆。当这些指令对项目协作者都有用时，可以提交到版本库。 |
| `<config-dir>/AGENTS.md` | 用户记忆。它跟随 `IAC_CODE_CONFIG_DIR`，只属于本地用户。 |

可以设置 `IAC_CODE_INSTRUCTION_MEMORY_FILE` 使用其他指令记忆文件名，例如 `IAC-CODE.md`。

项目 auto-memory topic 文件存放在：

```text
<config-dir>/projects/<project-key>/memory/
```

该文件夹中的 `MEMORY.md` 是供 auto-memory side call 使用的 topic 索引，不会作为常驻上下文加载。auto-memory 开启时，IaC Code 可能选择相关 topic 文件，并把它们作为隐藏会话上下文加入对话。

## 项目级设置

除了用户级的 `~/.iac-code/settings.yml`，IaC Code 还会从当前工作目录加载项目级设置：

| 文件 | 作用范围 |
|---|---|
| `.iac-code/settings.yml` | 项目共享设置（可以提交到版本库）。 |
| `.iac-code/settings.local.yml` | 本地覆盖（应加入 .gitignore）。 |

合并顺序：**用户设置 → 项目设置 → 项目本地设置 → CLI 参数**（后者覆盖前者）。

## Provider 请求策略

`settings.yml` 中的 provider 配置可以为 OpenAI 兼容类 provider 设置请求策略字段。当模型把可见回答 token 和 reasoning/thinking token 分开计算时，这些配置很有用。

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingEnabled: true
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingEnabled: false
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| 字段 | 作用范围 | 说明 |
|---|---|---|
| `thinkingEnabled` | Provider 或模型 | 可选的布尔 thinking 开关。`true` 表示请求支持的 provider/model 开启 thinking；`false` 表示请求关闭；省略时保留 provider/model 默认行为。 |
| `thinkingBudget` | Provider 或模型 | 正整数 reasoning/thinking 预算，会传给支持该参数的 provider。 |
| `maxCompletionTokens` | Provider 或模型 | 正整数 `max_completion_tokens` 覆盖值，用于采用该请求字段的 provider/model。 |
| `effort` | Provider 或模型 | 可选 thinking effort 覆盖值，仅对支持 effort 控制的模型生效。 |

`providers.<provider>.models.<model>` 下的模型级有效值会覆盖 provider 级值。无效数值会被忽略，IaC Code 会回退到 provider 级值或内置模型策略。

对阿里云百炼 DashScope 和 DashScope Token Plan，IaC Code 为 `glm-5.2` 和 `kimi-k2.7-code` 内置了 `thinkingBudget=8192`。未设置 `maxCompletionTokens` 时，请求上限会按普通回答 token 上限加上有效 thinking budget 计算。

A2A 请求可以通过 `message.metadata.iac_code.thinking` 或 `iac-code a2a-client call` 的 `--thinking-enabled`、`--thinking-effort`、`--thinking-budget` flag 在单次 message turn 覆盖这些设置。如果没有发送显式 A2A thinking metadata，runtime 会使用上述配置和 provider 自身默认行为。对于 base URL 指向 DashScope compatible-mode 的通用 `openai_compatible` provider，只有存在显式 thinking 策略时，iac-code 才会切换到 DashScope 原生 thinking wire format。

## 工具权限配置

`settings.yml` 中的 `permissions` 部分用于配置工具操作的允许、拒绝或需要确认的规则：

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| 字段 | 说明 |
|---|---|
| `mode` | 权限模式：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |
| `allow` | 自动批准的工具权限模式列表。 |
| `deny` | 自动拒绝的工具权限模式列表。 |
| `ask` | 始终需要确认的工具权限模式列表。 |
| `additional_directories` | 允许代理写入的额外目录（cwd 之外）。 |
| `audit` | 本地权限审计日志设置。 |

### 模式语法

工具权限模式遵循 `tool_name(rule)` 格式：

| 模式 | 含义 |
|---|---|
| `bash` | 匹配所有 bash 命令（裸工具名）。 |
| `bash(git *)` | 匹配以 `git` 开头的 bash 命令。 |
| `bash(curl:*)` | 匹配以 `curl` 开头的 bash 命令。 |
| `write_file` | 匹配所有 write_file 工具调用。 |
| `aliyun_api(ros:CreateStack)` | 匹配一个阿里云 API 产品/动作对。 |

规则按以下顺序评估：**deny → ask → allow → 默认行为**。CLI 参数（`--allowed-tools`、`--disallowed-tools`）具有最高优先级。

### 阿里云 API 权限

`aliyun_api` 会区分只读 API 调用和可能修改云资源的调用。只读 API 动作会自动允许。非只读 API 调用需要确认，或需要为该产品/动作配置精确允许规则，例如：

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

裸的 `aliyun_api` 允许规则不会一概批准阿里云写 API。在 `bypass_permissions` 之外，写入允许规则必须精确匹配规范化后的 `product:action`。在 `bypass_permissions` 模式下，受保护的阿里云写 API 会被自动批准，但任何需要审计记录的允许决策在审计持久化失败时都会 fail closed。通配符仍可用于 deny 或 ask 规则，也可用于只读规则匹配。

ROA 风格请求只有在方法为 `GET` 且请求没有 body 时才视为只读。非只读 ROA 请求与 RPC 风格写 API 一样，都必须精确匹配规范化后的 `product:action` 允许规则：形如 `aliyun_api(cs:CreateCluster)` 的精确规则可以批准该写操作，但通配允许规则仍不会批准非只读调用。

### 权限审计日志

跨越用户提示、工具缓存边界、自动化批准或 resolver 批准的权限决策会追加写入：

```text
<config-dir>/logs/permission-audit.jsonl
```

默认情况下，该路径是 `~/.iac-code/logs/permission-audit.jsonl`。权限审计日志跟随 `IAC_CODE_CONFIG_DIR`；`IAC_CODE_LOG_DIR` 只移动启动/调试日志。审计写入器会用文件锁追加 JSONL 记录，执行日志轮转，并在操作系统支持时限制本地文件权限。常规只读自动允许决策可能会省略，但拒绝、提示、缓存决策、自动化批准、resolver 批准以及其他被审计的权限边界都会记录。

审计设置位于 `permissions.audit` 下：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `include_tool_input` | `false` | 在 JSONL 审计记录中包含仅保留形态的工具输入。字符串会记录为类型、长度和指纹；疑似密钥的字段会被隐藏；非白名单字段名可能以指纹表示；不会写入业务原文。阿里云 API 条目还会保留安全的操作摘要。 |
| `max_file_bytes` | `10485760` | 当 `permission-audit.jsonl` 超过此大小时进行轮转。 |
| `max_files` | `5` | 保留的轮转审计文件数量。超过内置上限的值会被截断到上限。 |

如果任何需要审计记录的允许决策无法持久化到审计日志，IaC Code 会 fail closed，拒绝该操作，而不是在没有审计轨迹的情况下执行。
