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

OpenAI-style providers use Chat Completions by default. A model's built-in protocol default may select Responses when needed for its tool-calling capabilities. Model defaults and Chat tool restrictions are maintained in the built-in model catalog; they are not listed separately here. Anthropic providers continue to use their existing Messages protocol.

Providers that use the OpenAI protocol can explicitly select Responses for individual models, including Azure OpenAI, `openai_compatible`, and DashScope Token Plan. Configure `apiBase` with a base URL whose service supports Responses. For Azure OpenAI, use the `/openai/v1/` base URL and set `model` to the deployment name.

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) and [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) document Responses support for applicable endpoints and models. Custom compatible endpoints and coding plans depend on their service capabilities. [Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) explicitly does not support Responses. Selecting Responses for an unsupported service or model returns an error; IaC Code does not automatically downgrade to Chat.

To opt a supported Qwen model into Responses, add this model-level setting to `settings.yml`, replacing `<model-id>` with the actual model ID:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

Set `apiMode` to `chat_completions` to switch back. Omitting it restores the model's built-in protocol default; for standard DashScope models, that default remains Chat Completions. The setting applies only to the named model under the named provider.

If you explicitly select Chat, the model must support tool calls with the selected reasoning effort. Use Responses when the model's Chat API does not support that combination.

See [runtime configuration](./runtime-configuration.md) for request settings and output limits.
