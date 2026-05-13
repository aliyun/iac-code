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
| `-r <session-id>`, `--resume <session-id>` | 按会话 ID 恢复历史会话。适合回到已知的对话。 |
| `-c`, `--continue` | 恢复最近一次会话。不能与 `--resume` 同时使用。 |

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
