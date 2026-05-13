---
title: LLM Providers
description: Supported model providers and environment variables.
---

# LLM Providers

IaC Code supports multiple model provider backends.

| Provider value | Purpose |
|---|---|
| `Anthropic` | Anthropic models |
| `OpenAI` | OpenAI models |
| `DashScope` | Alibaba Cloud DashScope compatible endpoint |
| `DeepSeek` | DeepSeek models |
| `OpenAPICompatible` | Custom OpenAI-compatible endpoint |

Provider selection can come from CLI options, environment variables, or configuration files. Precedence is:

```text
CLI arguments > environment variables > configuration files
```

LLM environment variables:

| Variable | Description |
|---|---|
| `IAC_CODE_PROVIDER` | Model provider name, case-insensitive |
| `IAC_CODE_MODEL` | Model name |
| `IAC_CODE_BASE_URL` | API endpoint for `OpenAPICompatible` |
| `IAC_CODE_API_KEY` | Provider API key |
