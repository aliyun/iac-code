---
title: 命令行选项
description: IaC Code 启动选项和一次性执行参数参考。
---

# 命令行选项

根命令默认启动 Web。下表介绍 Web 启动、单次 headless 执行和 stream-json 子进程的参数；交互会话的选项在 Web 或桌面版中设置。

“适用入口”列说明每个参数可用的位置。`headless` 指 `iac-code --prompt ...`；`process` 指 stream-json 子进程模式。Web 的模型和权限选项在界面中设置。

| 选项 | 用途 | 适用入口 |
|---|---|---|
| `-h`, `--help` | 显示 CLI 帮助并退出。用它查看当前安装版本支持的选项。 | `iac-code`, `web` |
| `-v`, `-V`, `--version` | 输出已安装的 IaC Code 版本并退出。 | `iac-code` |
| `-m <model>`, `--model <model>` | 使用指定的 LLM 模型启动。本次运行会覆盖已保存的模型设置。 | `headless`, `process` |
| `-p <prompt>`, `--prompt <prompt>` | 执行单条提示词并退出。这会进入非交互模式。使用 `--prompt -` 可以从标准输入读取提示词。 | `iac-code` |
| `--output-format <format>` | 设置一次性调用或 process 自动化的输出格式。支持 `text`、`json` 和 `stream-json`，默认值为 `text`。 | `headless`, `process` |
| `--input-format <format>` | 设置输入格式。支持 `text` 和 `stream-json`。`stream-json` 会启动 SDK process 模式，必须与 `--output-format stream-json` 一起使用，且不能与 `--prompt` 组合。 | `headless`, `process` |
| `--max-turns <number>` | 限制非交互模式中的最大代理轮次，默认值为 `100`。 | `headless`, `process` |
| `--thinking-enabled`, `--no-thinking-enabled` | 控制一次性非交互请求是否显式启用 thinking。默认值为 `--thinking-enabled`；使用 `--no-thinking-enabled` 会在本次运行中发送 `thinking_enabled=false`，但不会改写 `settings.yml`。 | `headless` |
| `-d`, `--debug` | 为本次运行开启调试日志。 | `headless`, `process`, `web` |
| `--verbose` | 在标准错误输出上显示 headless（非交互）模式的进度。 | `headless` |
| `-r <session-id-or-name>`, `--resume <session-id-or-name>` | 按精确会话 ID、唯一 ID 前缀或唯一名称恢复会话。headless 命令应从会话所属的项目目录运行。 | `headless` |
| `-c`, `--continue` | 恢复最近一次会话。不能与 `--resume` 同时使用。 | `headless` |
| `--allowed-tools <patterns>` | 逗号分隔的工具权限允许模式，例如 `'bash(git *),write_file'`。 | `headless`, `process` |
| `--disallowed-tools <patterns>` | 逗号分隔的工具权限拒绝模式，例如 `'bash(rm *)'`。 | `headless`, `process` |
| `--permission-mode <mode>` | 权限模式：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 | `headless`, `process` |

## 权限模式

`--permission-mode` 参数控制代理处理工具权限检查的方式：

| 模式 | 行为 |
|---|---|
| `default` | 当工具操作需要确认时，代理会弹出提示让用户选择。 |
| `accept_edits` | 自动批准被视为编辑操作的文件系统命令（如 `mkdir`、`cp`），其他操作仍需确认。 |
| `bypass_permissions` | 自动批准工具操作（安全检查除外）。任何需要审计记录的允许决策在审计持久化失败时都会 fail closed。适用于可信的自动化场景。 |
| `dont_ask` | 拒绝需要审批的工具操作；匹配显式允许规则的操作仍可执行。 |

通过 `aliyun_api` 发起的阿里云写 API 调用有单独保护：裸的 `aliyun_api` 允许规则不会一概批准这些调用，并且在 `bypass_permissions` 之外，写入允许规则必须精确匹配规范化后的 `product:action`。`bypass_permissions` 会自动批准它们，但与其他被审计的允许决策一样，如果权限审计记录无法持久化，该操作会被拒绝。需要在可信自动化中只允许某个具体写 API 时，请使用 `aliyun_api(ros:CreateStack)` 这样的精确规则。

## 常用启动命令

启动本地 Web 应用：

```bash
iac-code
```

为本次运行指定模型：

```bash
iac-code --prompt "创建一个 VPC" --model qwen3.7-max
```

执行一次性提示词：

```bash
iac-code --prompt "创建一个 OSS Bucket"
```

执行一次性提示词，并只在本次请求中关闭 thinking：

```bash
iac-code --prompt "创建一个 OSS Bucket" --no-thinking-enabled
```

从标准输入读取提示词：

```bash
echo "创建一个 VPC 和两台 ECS 实例" | iac-code --prompt -
```

以单条提示词继续最近一次会话：

```bash
iac-code --continue --prompt "生成一个 VPC 模板"
```

预先允许匹配 `git *` 的 bash 命令：

```bash
iac-code --prompt "查看 git 状态" --allowed-tools 'bash(git *)'
```

预先允许一个具体的阿里云写 API：

```bash
iac-code --prompt "部署资源栈" --allowed-tools 'aliyun_api(ros:CreateStack)'
```

在自动化中运行，无需交互确认：

```bash
iac-code --prompt "创建一个 VPC" --permission-mode bypass_permissions
```

为长期运行的子进程客户端启动 SDK process 模式：

```bash
iac-code --input-format stream-json --output-format stream-json
```

在 process 模式下，调用方向 stdin 写入一行一个 JSON frame，并从 stdout 读取一行一个 JSON frame。发送用户消息前需要先发送 `initialize` control request。该模式支持 `interrupt`、`set_model`、`end_session`、`close`、`keep_alive` 和 `update_environment_variables` 等控制流程。当 `IAC_CODE_MODE=pipeline` 时，同一个 process 入口会运行 Pipeline 模式，并在 stream 和 result frame 中返回 pipeline 状态。
