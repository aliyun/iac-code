---
title: CLI 概览
description: 从终端启动 IaC Code，并选择合适的工作方式。
---

# CLI 概览

从终端运行 `iac-code`：

```bash
iac-code
```

CLI 支持两种工作方式：

| 工作方式 | 适用场景 |
|---|---|
| [交互模式](./interactive-mode.md) | 需要在 REPL 中通过多轮对话细化基础设施需求。 |
| [非交互模式](../automation/non-interactive-mode.md) | 需要执行单条提示词，并把输出返回给调用方。 |

常用启动命令：

```bash
iac-code
iac-code --prompt "创建一个 OSS Bucket"
echo "创建一个 VPC" | iac-code --prompt -
iac-code --debug
```

启动参数请参见[命令行选项](./command-line-options.md)，交互会话内可用命令请参见[Slash 命令](./commands.md)。
