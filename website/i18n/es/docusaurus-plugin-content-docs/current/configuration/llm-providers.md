---
title: Proveedores de LLM
description: Proveedores de modelos soportados y variables de entorno.
---

# Proveedores de LLM

IaC Code admite multiples backends de proveedores de modelos. La seleccion del proveedor puede provenir de las opciones del CLI, variables de entorno o archivos de configuracion. La precedencia es:

```text
CLI arguments > environment variables > configuration files
```

## Proveedores en la Nube

| Valor del proveedor | Proposito |
|---|---|
| `DashScope` | Endpoint compatible con Alibaba Cloud DashScope (Bailian) |
| `DashScope Token Plan` | Endpoint Alibaba Cloud DashScope Token Plan |
| `OpenAI` | Modelos de OpenAI |
| `Anthropic` | Modelos de Anthropic |
| `DeepSeek` | Modelos de DeepSeek |
| `Gemini` | Modelos de Google Gemini |
| `Azure OpenAI` | Servicio Azure OpenAI |
| `ModelScope` | Endpoint de inferencia ModelScope |

## Proveedores en China

| Valor del proveedor | Proposito |
|---|---|
| `Kimi CN` | Endpoint Kimi (Moonshot AI) en China |
| `MiniMax CN` | Endpoint MiniMax en China |
| `ZhiPu CN` | Endpoint ZhiPu AI (GLM) en China |
| `Volcengine CN` | Endpoint Volcengine (ByteDance) en China |
| `SiliconFlow CN` | Endpoint SiliconFlow en China |

## Proveedores Internacionales

| Valor del proveedor | Proposito |
|---|---|
| `Kimi Intl` | Endpoint internacional Kimi (Moonshot AI) |
| `MiniMax Intl` | Endpoint internacional MiniMax |
| `ZhiPu Intl` | Endpoint internacional ZhiPu AI (GLM) |
| `SiliconFlow Intl` | Endpoint internacional SiliconFlow |

## Proveedores CodingPlan

| Valor del proveedor | Proposito |
|---|---|
| `Aliyun CodingPlan` | Endpoint Alibaba Cloud CodingPlan |
| `Aliyun CodingPlan Intl` | Endpoint internacional Alibaba Cloud CodingPlan |
| `ZhiPu CN CodingPlan` | Endpoint ZhiPu AI CodingPlan en China |
| `ZhiPu Intl CodingPlan` | Endpoint internacional ZhiPu AI CodingPlan |
| `Volcengine CodingPlan` | Endpoint Volcengine CodingPlan |

## Compatible / Endpoints Personalizados

| Valor del proveedor | Proposito |
|---|---|
| `OpenAPI Compatible` | Cualquier endpoint de API compatible con OpenAI |
| `Anthropic Compatible` | Cualquier endpoint de API compatible con Anthropic |
| `OpenRouter` | Gateway de agregacion OpenRouter |

## Proveedores Locales

| Valor del proveedor | Proposito |
|---|---|
| `Ollama` | Servidor de modelos local Ollama |
| `LM Studio` | Servidor de modelos local LM Studio |

## Variables de Entorno de LLM

| Variable | Descripcion |
|---|---|
| `IAC_CODE_PROVIDER` | Nombre del proveedor de modelos (sin distincion de mayusculas/minusculas). Consulta las tablas anteriores para valores validos |
| `IAC_CODE_MODEL` | Nombre del modelo |
| `IAC_CODE_BASE_URL` | Sobrescribe el endpoint de API del proveedor activo; tiene prioridad sobre el `apiBase` guardado y la URL predeterminada integrada |
| `IAC_CODE_API_KEY` | Clave API del proveedor |

## Responses API

Los proveedores que usan el protocolo de OpenAI utilizan Chat Completions de forma predeterminada. Los modelos OpenAI registrados `gpt-6-astra`, `gpt-6-sol` y `gpt-6-luna` utilizan Responses API de forma predeterminada. Los proveedores Anthropic conservan su protocolo Messages actual.

Actualmente, Responses está disponible para el endpoint oficial de OpenAI y los endpoints y modelos verificados de DashScope estándar, como `qwen3.8-max`. Azure OpenAI, `openai_compatible`, DashScope Token Plan y los proveedores CodingPlan no admiten este cambio.

Para usar Responses con un modelo Qwen, añada esta configuración a nivel de modelo en `settings.yml`:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: qwen3.8-max
    models:
      qwen3.8-max:
        apiMode: responses
```

Cambie `apiMode` a `chat_completions` para volver al protocolo anterior. Si lo omite, se utiliza el protocolo predeterminado del modelo; para los modelos de DashScope estándar sigue siendo Chat Completions. La configuración solo se aplica al modelo indicado dentro del proveedor indicado.

Si selecciona Chat explícitamente para GPT-6, Astra no puede llamar a herramientas; Sol y Luna solo pueden hacerlo con `effort: none`. Use Responses para llamadas a herramientas con razonamiento habilitado.

Consulte la [configuración de ejecución](./runtime-configuration.md) para conocer los parámetros de solicitud y los límites de salida.
