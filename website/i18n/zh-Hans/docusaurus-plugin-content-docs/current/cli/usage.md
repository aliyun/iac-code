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

[交互模式](./interactive-mode.md):

```bash
iac-code tui
iac-code tui --help
```

`iac-code` 默认打开 Web。终端参数放在 `iac-code tui` 后面，例如 `iac-code tui --resume <id>`。headless 的 `--prompt` 和 stream-json 参数仍放在根命令上。没有交互终端时，请显式运行 `iac-code web --no-open`。

```bash
iac-code --prompt "创建一个 OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
