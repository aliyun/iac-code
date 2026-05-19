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
| history files | 交互式工作流的输入历史 |

避免提交或分享该目录中的文件，因为它们可能包含密钥或本地偏好。

## 项目级设置

除了用户级的 `~/.iac-code/settings.yml`，IaC Code 还会从当前工作目录加载项目级设置：

| 文件 | 作用范围 |
|---|---|
| `.iac-code/settings.yml` | 项目共享设置（可以提交到版本库）。 |
| `.iac-code/settings.local.yml` | 本地覆盖（应加入 .gitignore）。 |

合并顺序：**用户设置 → 项目设置 → 项目本地设置 → CLI 参数**（后者覆盖前者）。

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
