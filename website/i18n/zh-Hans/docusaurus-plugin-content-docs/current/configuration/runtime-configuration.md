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

可通过设置 `IAC_CODE_CONFIG_DIR` 环境变量更改该目录（支持 `~` 和 `$VAR` 展开）。设置后，所有持久化产物——凭证、设置、历史、`projects/`、`image-cache/`、`tool-results/`、`logs/`、`memory/`、`a2a/`、`telemetry/`、`skills/`——都会跟随到新位置。

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
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| 字段 | 作用范围 | 说明 |
|---|---|---|
| `thinkingBudget` | Provider 或模型 | 正整数 reasoning/thinking 预算，会传给支持该参数的 provider。 |
| `maxCompletionTokens` | Provider 或模型 | 正整数 `max_completion_tokens` 覆盖值，用于采用该请求字段的 provider/model。 |
| `effort` | Provider 或模型 | 可选 thinking effort 覆盖值，仅对支持 effort 控制的模型生效。 |

`providers.<provider>.models.<model>` 下的模型级有效值会覆盖 provider 级值。无效数值会被忽略，IaC Code 会回退到 provider 级值或内置模型策略。

对阿里云百炼 DashScope 和 DashScope Token Plan，IaC Code 为 `glm-5.2` 和 `kimi-k2.7-code` 内置了 `thinkingBudget=8192`。未设置 `maxCompletionTokens` 时，请求上限会按普通回答 token 上限加上有效 thinking budget 计算。

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
```

| 字段 | 说明 |
|---|---|
| `mode` | 权限模式：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |
| `allow` | 自动批准的工具权限模式列表。 |
| `deny` | 自动拒绝的工具权限模式列表。 |
| `ask` | 始终需要确认的工具权限模式列表。 |
| `additional_directories` | 允许代理写入的额外目录（cwd 之外）。 |

### 模式语法

工具权限模式遵循 `tool_name(rule)` 格式：

| 模式 | 含义 |
|---|---|
| `bash` | 匹配所有 bash 命令（裸工具名）。 |
| `bash(git *)` | 匹配以 `git` 开头的 bash 命令。 |
| `bash(curl:*)` | 匹配以 `curl` 开头的 bash 命令。 |
| `write_file` | 匹配所有 write_file 工具调用。 |

规则按以下顺序评估：**deny → ask → allow → 默认行为**。CLI 参数（`--allowed-tools`、`--disallowed-tools`）具有最高优先级。
