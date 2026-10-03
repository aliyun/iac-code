---
title: LLM Providers
description: Supported model providers and environment variables.
---

# LLM Providers

IaC Code supports multiple model provider backends. Provider selection can come from CLI options, environment variables, or configuration files. Precedence is:

```text
CLI arguments > environment variables > configuration files
```

## Cloud Providers

| Provider value | Purpose |
|---|---|
| `DashScope` | Alibaba Cloud DashScope (Bailian) compatible endpoint |
| `DashScope Token Plan` | Alibaba Cloud DashScope Token Plan endpoint |
| `OpenAI` | OpenAI models |
| `Anthropic` | Anthropic models |
| `DeepSeek` | DeepSeek models |
| `Gemini` | Google Gemini models |
| `Azure OpenAI` | Azure OpenAI Service |
| `ModelScope` | ModelScope inference endpoint |

## China-region Providers

| Provider value | Purpose |
|---|---|
| `Kimi CN` | Kimi (Moonshot AI) China endpoint |
| `MiniMax CN` | MiniMax China endpoint |
| `ZhiPu CN` | ZhiPu AI (GLM) China endpoint |
| `Volcengine CN` | Volcengine (ByteDance) China endpoint |
| `SiliconFlow CN` | SiliconFlow China endpoint |

## International Providers

| Provider value | Purpose |
|---|---|
| `Kimi Intl` | Kimi (Moonshot AI) international endpoint |
| `MiniMax Intl` | MiniMax international endpoint |
| `ZhiPu Intl` | ZhiPu AI (GLM) international endpoint |
| `SiliconFlow Intl` | SiliconFlow international endpoint |

## CodingPlan Providers

| Provider value | Purpose |
|---|---|
| `Aliyun CodingPlan` | Alibaba Cloud CodingPlan endpoint |
| `Aliyun CodingPlan Intl` | Alibaba Cloud CodingPlan international endpoint |
| `ZhiPu CN CodingPlan` | ZhiPu AI CodingPlan China endpoint |
| `ZhiPu Intl CodingPlan` | ZhiPu AI CodingPlan international endpoint |
| `Volcengine CodingPlan` | Volcengine CodingPlan endpoint |

## Compatible / Custom Endpoints

| Provider value | Purpose |
|---|---|
| `OpenAI Compatible` | Any OpenAI-compatible API endpoint |
| `Anthropic Compatible` | Any Anthropic-compatible API endpoint |
| `OpenRouter` | OpenRouter aggregation gateway |

## Local Providers

| Provider value | Purpose |
|---|---|
| `Ollama` | Ollama local model server |
| `LM Studio` | LM Studio local model server |

## LLM Environment Variables

| Variable | Description |
|---|---|
| `IAC_CODE_PROVIDER` | Model provider name (case-insensitive). See tables above for valid values |
| `IAC_CODE_MODEL` | Model name |
| `IAC_CODE_BASE_URL` | API endpoint override for the active provider; takes precedence over the saved `apiBase` and built-in default URL |
| `IAC_CODE_API_KEY` | Provider API key |

## Responses API

OpenAI-style providers use Chat Completions by default. The registered OpenAI models `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna` default to Responses API. Anthropic providers continue to use their existing Messages protocol.

Responses is currently available for official OpenAI and verified standard DashScope endpoints and models, such as `qwen3.8-max`. Azure OpenAI, `openai_compatible`, DashScope Token Plan, and CodingPlan providers do not support this switch.

To opt a Qwen model into Responses, add this model-level setting to `settings.yml`:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: qwen3.8-max
    models:
      qwen3.8-max:
        apiMode: responses
```

Set `apiMode` to `chat_completions` to switch back. Omitting it restores the model's built-in protocol default; for standard DashScope models, that default remains Chat Completions. The setting applies only to the named model under the named provider.

If you explicitly choose Chat for GPT-6, Astra cannot make tool calls; Sol and Luna can make tool calls only with `effort: none`. Use Responses for tool calls with reasoning enabled.

See [runtime configuration](./runtime-configuration.md) for request settings and output limits.
