---
title: LLM Providers
description: Supported model providers and environment variables.
---

# LLM Providers

The steps below apply to both Desktop and Web; they use the same conversation interface.

## Configure a provider in Desktop or Web

1. Open **Settings → Models**.

2. Select the provider group and then the provider on the left. Its configuration form appears on the right.

3. Enter or select **Models**, fill **API key** when required, and change **API base URL** only if needed. Leave the URL blank to use the provider default.

4. If supported, select **Reasoning effort** or expand **Advanced settings** to set the thinking budget.

5. Click **Save configuration** and wait for the saved message. To make this provider’s saved model the current default, click **Set as current model**.

6. Return to a conversation and check the model control below the composer. Use that control to choose the model for the conversation.

:::note

Saving a configuration and setting the current model are separate actions. Save edited fields before activating a provider.

:::

The provider catalog and advanced options are listed below. Environment variables and manual files are intended for scripts, services, or custom startup configuration; see [environment variables](./environment-variables.md) and [configuration files](./runtime-configuration.md).

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

See [runtime configuration](./runtime-configuration.md) for request settings.
