---
title: 命令行选项
description: IaC Code 启动选项和一次性执行参数参考。
---

# 命令行选项

命令行选项用于控制 IaC Code 的启动方式。它们可以在进入交互式 REPL 前使用，也可以与 `--prompt` 组合用于一次性自动化任务。

| 选项 | 用途 |
|---|---|
| `-h`, `--help` | 显示 CLI 帮助并退出。用它查看当前安装版本支持的选项。 |
| `-v`, `-V`, `--version` | 输出已安装的 IaC Code 版本并退出。 |
| `-m <model>`, `--model <model>` | 使用指定的 LLM 模型启动。本次运行会覆盖已保存的模型设置。 |
| `-p <prompt>`, `--prompt <prompt>` | 执行单条提示词并退出。这会进入非交互模式。使用 `--prompt -` 可以从标准输入读取提示词。 |
| `--output-format <format>` | 设置非交互模式的输出格式。支持 `text`、`json` 和 `stream-json`，默认值为 `text`。 |
| `--max-turns <number>` | 限制非交互模式中的最大代理轮次，默认值为 `100`。 |
| `-d`, `--debug` | 为本次运行启用调试日志。交互模式启动后，可以使用 `/debug` 查看或调整调试日志。 |
| `-r <session-id-or-name>`, `--resume <session-id-or-name>` | 按精确会话 ID、唯一 ID 前缀或唯一会话名称恢复历史会话。解析到跨项目会话时，会打印 `cd ... && iac-code --resume <id>` 命令，而不是直接热切换当前项目。 |
| `-c`, `--continue` | 恢复最近一次会话。不能与 `--resume` 同时使用。 |
| `--allowed-tools <patterns>` | 逗号分隔的工具权限允许模式，例如 `'bash(git *),write_file'`。 |
| `--disallowed-tools <patterns>` | 逗号分隔的工具权限拒绝模式，例如 `'bash(rm *)'`。 |
| `--permission-mode <mode>` | 权限模式：`default`、`accept_edits`、`bypass_permissions`、`dont_ask`。 |

## 权限模式

`--permission-mode` 参数控制代理处理工具权限检查的方式：

| 模式 | 行为 |
|---|---|
| `default` | 当工具操作需要确认时，代理会弹出提示让用户选择。 |
| `accept_edits` | 自动批准被视为编辑操作的文件系统命令（如 `mkdir`、`cp`），其他操作仍需确认。 |
| `bypass_permissions` | 自动批准所有工具操作（安全检查除外）。适用于可信的自动化场景。 |
| `dont_ask` | 静默拒绝所有需要确认的操作。适用于严格的只读运行。 |

## 常用启动命令

使用已保存的模型进入交互式 REPL：

```bash
iac-code
```

为本次运行指定模型：

```bash
iac-code --model qwen3.6-plus
```

执行一次性提示词：

```bash
iac-code --prompt "创建一个 OSS Bucket"
```

从标准输入读取提示词：

```bash
echo "创建一个 VPC 和两台 ECS 实例" | iac-code --prompt -
```

恢复最近一次会话：

```bash
iac-code --continue
```

仅允许 git 和只读 bash 命令：

```bash
iac-code --allowed-tools 'bash(git *)'
```

在自动化中运行，无需交互确认：

```bash
iac-code --prompt "创建一个 VPC" --permission-mode bypass_permissions
```
