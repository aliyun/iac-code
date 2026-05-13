---
title: 非交互模式
description: 从参数或 stdin 运行一次性提示词。
---

# 非交互模式

非交互模式会执行单条提示词并退出。它适合让 IaC Code 为可重复任务生成输出，而不进入 REPL。

使用 `--prompt` 直接传入提示词：

```bash
iac-code --prompt "创建一个 OSS Bucket"
```

使用 `--prompt -` 从标准输入读取提示词：

```bash
echo "创建一个 VPC 和两台 ECS 实例" | iac-code --prompt -
```

当调用方需要结构化输出时，使用 `--output-format`：

```bash
iac-code --prompt "创建一个 OSS Bucket" --output-format json
```

使用 `--max-turns` 限制代理最多可以工作多少轮：

```bash
iac-code --prompt "创建一个 VPC" --max-turns 20
```

支持的输出格式包括：

| 格式 | 用途 |
|---|---|
| `text` | 面向用户阅读的文本输出，默认使用该格式。 |
| `json` | 返回单个 JSON 结果，适合调用方解析最终响应。 |
| `stream-json` | 输出流式 JSON 事件，适合调用方处理增量进度。 |

完整启动参数请参见[命令行选项](../cli/command-line-options.md)。
