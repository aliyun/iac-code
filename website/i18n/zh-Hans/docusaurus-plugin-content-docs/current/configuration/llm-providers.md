---
title: LLM 提供商
description: 支持的模型提供商和环境变量。
---

# LLM 提供商

IaC Code 支持多种模型提供商后端。提供商选择可以来自 CLI 参数、环境变量或配置文件。优先级为：

```text
CLI 参数 > 环境变量 > 配置文件
```

## 云服务提供商

| 提供商值 | 用途 |
|---|---|
| `DashScope` | 阿里云百炼（DashScope）兼容端点 |
| `DashScope Token Plan` | 阿里云百炼 Token 计划端点 |
| `OpenAI` | OpenAI 模型 |
| `Anthropic` | Anthropic 模型 |
| `DeepSeek` | DeepSeek 模型 |
| `Gemini` | Google Gemini 模型 |
| `Azure OpenAI` | Azure OpenAI 服务 |
| `ModelScope` | 魔搭推理端点 |

## 国内提供商

| 提供商值 | 用途 |
|---|---|
| `Kimi CN` | Kimi（月之暗面）国内端点 |
| `MiniMax CN` | MiniMax 国内端点 |
| `ZhiPu CN` | 智谱 AI（GLM）国内端点 |
| `Volcengine CN` | 火山引擎（字节跳动）国内端点 |
| `SiliconFlow CN` | 硅基流动国内端点 |

## 国际提供商

| 提供商值 | 用途 |
|---|---|
| `Kimi Intl` | Kimi（月之暗面）国际端点 |
| `MiniMax Intl` | MiniMax 国际端点 |
| `ZhiPu Intl` | 智谱 AI（GLM）国际端点 |
| `SiliconFlow Intl` | 硅基流动国际端点 |

## CodingPlan 提供商

| 提供商值 | 用途 |
|---|---|
| `Aliyun CodingPlan` | 阿里云 CodingPlan 端点 |
| `Aliyun CodingPlan Intl` | 阿里云 CodingPlan 国际端点 |
| `ZhiPu CN CodingPlan` | 智谱 AI CodingPlan 国内端点 |
| `ZhiPu Intl CodingPlan` | 智谱 AI CodingPlan 国际端点 |
| `Volcengine CodingPlan` | 火山引擎 CodingPlan 端点 |

## 兼容 / 自定义端点

| 提供商值 | 用途 |
|---|---|
| `OpenAPI Compatible` | 任意 OpenAI 兼容 API 端点 |
| `Anthropic Compatible` | 任意 Anthropic 兼容 API 端点 |
| `OpenRouter` | OpenRouter 聚合网关 |

## 本地提供商

| 提供商值 | 用途 |
|---|---|
| `Ollama` | Ollama 本地模型服务 |
| `LM Studio` | LM Studio 本地模型服务 |

## LLM 环境变量

| 变量 | 说明 |
|---|---|
| `IAC_CODE_PROVIDER` | 模型提供商名称（大小写不敏感），有效值见上表 |
| `IAC_CODE_MODEL` | 模型名称 |
| `IAC_CODE_BASE_URL` | 当前激活 Provider 的 API 端点覆盖；优先于配置文件中的 `apiBase` 和内置默认 URL |
| `IAC_CODE_API_KEY` | 提供商 API Key |

## Responses API

使用 OpenAI 风格协议的提供商默认走 Chat Completions；部分模型会根据工具调用能力内置选择 Responses 作为默认协议。各模型的默认协议和 Chat 工具调用限制由内置模型目录统一维护，此处不另列模型名单。Anthropic 提供商继续使用原有的 Messages 协议。

使用 OpenAI 协议的提供商可按模型显式选择 Responses，包括 Azure OpenAI、`openai_compatible` 和百炼 Token Plan。`apiBase` 应配置为支持 Responses 的服务基础地址。Azure OpenAI 使用 `/openai/v1/` 基础地址，`model` 填写部署名称。

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) 和[百炼 Token Plan](https://help.aliyun.com/zh/model-studio/codex) 已有支持 Responses 的端点和模型说明。自定义兼容端点及编程套餐是否支持，取决于具体服务。[阿里云 Coding Plan](https://help.aliyun.com/zh/model-studio/coding-plan-faq) 明确不支持 Responses。若为不支持的服务或模型配置 Responses，请求会报错，iac-code 不会自动降级到 Chat。

要让受支持的 Qwen 模型改用 Responses，在 `settings.yml` 中添加模型级配置，并将 `<model-id>` 替换为实际模型 ID：

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

把 `apiMode` 改为 `chat_completions` 即可切回；省略该项则使用模型内置的默认协议，标准百炼模型仍默认为 Chat Completions。该配置只作用于指定提供商下的指定模型。

显式选择 Chat 时，模型须支持当前推理强度下的工具调用。若模型的 Chat API 不支持该组合，应使用 Responses。

请求参数和输出限制见[运行配置](./runtime-configuration.md)。
