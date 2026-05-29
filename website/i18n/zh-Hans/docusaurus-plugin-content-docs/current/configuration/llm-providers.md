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
| `IAC_CODE_BASE_URL` | `OpenAPI Compatible` 和 `Anthropic Compatible` 使用的 API 端点；其他提供商会忽略此值 |
| `IAC_CODE_API_KEY` | 提供商 API Key |
