---
title: Provedores de LLM
description: Provedores de modelos suportados e variaveis de ambiente.
---

# Provedores de LLM

O IaC Code suporta multiplos backends de provedores de modelos. A selecao do provedor pode vir de opcoes do CLI, variaveis de ambiente ou arquivos de configuracao. A precedencia e:

```text
CLI arguments > environment variables > configuration files
```

## Provedores na Nuvem

| Valor do provedor | Finalidade |
|---|---|
| `DashScope` | Endpoint compativel com Alibaba Cloud DashScope (Bailian) |
| `DashScope Token Plan` | Endpoint Alibaba Cloud DashScope Token Plan |
| `OpenAI` | Modelos OpenAI |
| `Anthropic` | Modelos Anthropic |
| `DeepSeek` | Modelos DeepSeek |
| `Gemini` | Modelos Google Gemini |
| `Azure OpenAI` | Servico Azure OpenAI |
| `ModelScope` | Endpoint de inferencia ModelScope |

## Provedores na China

| Valor do provedor | Finalidade |
|---|---|
| `Kimi CN` | Endpoint Kimi (Moonshot AI) na China |
| `MiniMax CN` | Endpoint MiniMax na China |
| `ZhiPu CN` | Endpoint ZhiPu AI (GLM) na China |
| `Volcengine CN` | Endpoint Volcengine (ByteDance) na China |
| `SiliconFlow CN` | Endpoint SiliconFlow na China |

## Provedores Internacionais

| Valor do provedor | Finalidade |
|---|---|
| `Kimi Intl` | Endpoint internacional Kimi (Moonshot AI) |
| `MiniMax Intl` | Endpoint internacional MiniMax |
| `ZhiPu Intl` | Endpoint internacional ZhiPu AI (GLM) |
| `SiliconFlow Intl` | Endpoint internacional SiliconFlow |

## Provedores CodingPlan

| Valor do provedor | Finalidade |
|---|---|
| `Aliyun CodingPlan` | Endpoint Alibaba Cloud CodingPlan |
| `Aliyun CodingPlan Intl` | Endpoint internacional Alibaba Cloud CodingPlan |
| `ZhiPu CN CodingPlan` | Endpoint ZhiPu AI CodingPlan na China |
| `ZhiPu Intl CodingPlan` | Endpoint internacional ZhiPu AI CodingPlan |
| `Volcengine CodingPlan` | Endpoint Volcengine CodingPlan |

## Compativeis / Endpoints Personalizados

| Valor do provedor | Finalidade |
|---|---|
| `OpenAPI Compatible` | Qualquer endpoint de API compativel com OpenAI |
| `Anthropic Compatible` | Qualquer endpoint de API compativel com Anthropic |
| `OpenRouter` | Gateway de agregacao OpenRouter |

## Provedores Locais

| Valor do provedor | Finalidade |
|---|---|
| `Ollama` | Servidor de modelo local Ollama |
| `LM Studio` | Servidor de modelo local LM Studio |

## Variaveis de Ambiente de LLM

| Variavel | Descricao |
|---|---|
| `IAC_CODE_PROVIDER` | Nome do provedor de modelo (insensivel a maiusculas e minusculas). Consulte as tabelas acima para valores validos |
| `IAC_CODE_MODEL` | Nome do modelo |
| `IAC_CODE_BASE_URL` | Endpoint de API para `OpenAPI Compatible` e `Anthropic Compatible` apenas; ignorado para outros provedores |
| `IAC_CODE_API_KEY` | Chave de API do provedor |
