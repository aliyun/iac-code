---
title: LLM 提供商
description: 支持的模型提供商和环境变量。
---

# LLM 提供商

IaC Code 支持多种模型提供商后端。

| 提供商值 | 用途 |
|---|---|
| `Anthropic` | Anthropic 模型 |
| `OpenAI` | OpenAI 模型 |
| `DashScope` | 阿里云百炼兼容端点 |
| `DeepSeek` | DeepSeek 模型 |
| `OpenAPICompatible` | 自定义 OpenAI 兼容端点 |

提供商选择可以来自 CLI 参数、环境变量或配置文件。优先级为：

```text
CLI 参数 > 环境变量 > 配置文件
```

LLM 环境变量：

| 变量 | 说明 |
|---|---|
| `IAC_CODE_PROVIDER` | 模型提供商名称，大小写不敏感 |
| `IAC_CODE_MODEL` | 模型名称 |
| `IAC_CODE_BASE_URL` | `OpenAPICompatible` 使用的 API 端点 |
| `IAC_CODE_API_KEY` | 提供商 API Key |
