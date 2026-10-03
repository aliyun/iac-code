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
| `IAC_CODE_BASE_URL` | Substitui o endpoint de API do provedor ativo; tem precedência sobre o `apiBase` salvo e a URL padrão integrada |
| `IAC_CODE_API_KEY` | Chave de API do provedor |

## Responses API

Os provedores que usam o protocolo OpenAI utilizam Chat Completions por padrão. Um modelo pode ter Responses como protocolo padrão integrado quando suas chamadas de ferramentas exigem isso. Os protocolos padrão e as restrições de ferramentas em Chat são mantidos no catálogo integrado de modelos; não há uma lista separada nesta página. Os provedores Anthropic continuam usando o protocolo Messages atual.

Os provedores que utilizam o protocolo OpenAI podem selecionar Responses explicitamente para cada modelo, incluindo Azure OpenAI, `openai_compatible` e DashScope Token Plan. Configure `apiBase` com a URL base de um serviço que ofereça suporte a Responses. Para Azure OpenAI, use a URL base com `/openai/v1/` e defina `model` como o nome da implantação.

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) e [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) documentam o suporte a Responses para determinados endpoints e modelos. Nos endpoints compatíveis personalizados e nos planos de programação, o suporte depende do serviço utilizado. [Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) informa explicitamente que não oferece suporte a Responses. Selecionar Responses para um serviço ou modelo incompatível faz a solicitação retornar um erro; o IaC Code não muda automaticamente para Chat.

Para usar Responses com um modelo Qwen compatível, adicione esta configuração no nível do modelo em `settings.yml` e substitua `<model-id>` pelo ID real do modelo:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

Defina `apiMode` como `chat_completions` para voltar ao protocolo anterior. Se o campo for omitido, será usado o protocolo padrão integrado do modelo; para modelos do DashScope padrão, ele continua sendo Chat Completions. A configuração se aplica apenas ao modelo indicado no provedor indicado.

Se você selecionar Chat explicitamente, o modelo deve oferecer suporte a chamadas de ferramentas com o nível de raciocínio escolhido. Use Responses quando a API Chat do modelo não oferecer suporte a essa combinação.

Consulte a [configuração de execução](./runtime-configuration.md) para ver os parâmetros de requisição e os limites de saída.
