---
title: CLI 概览
description: 从终端启动 IaC Code，并选择合适的工作方式。
---

# CLI 概览

在终端运行 `iac-code`，即可启动本地 Web 应用并打开浏览器。首次使用时，在设置中配置 LLM 提供商和阿里云凭证。

```bash
iac-code
iac-code web --no-open
```

在 Web 或桌面版中新建会话时选择普通对话或 Pipeline。自动化场景可使用单条提示词或 stream-json 子进程：

```bash
iac-code --prompt "生成一个 VPC 模板"
iac-code --input-format stream-json --output-format stream-json
```

参数说明见[命令行选项](./command-line-options.md)和[非交互自动化](../automation/non-interactive-mode.md)。没有交互终端时，请显式运行 `iac-code web --no-open` 启动 Web。

已有终端工作流的兼容方式见[旧终端入口](./interactive-mode.md)。
