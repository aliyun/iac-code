---
title: Proveedores de LLM
description: Proveedores de modelos soportados y variables de entorno.
---

# Proveedores de LLM

Los pasos se aplican tanto a Desktop como a Web, que comparten la misma interfaz de conversación.

## Configurar un proveedor en Desktop o Web

1. Abre **Ajustes → Modelos**.

2. Elige a la izquierda el grupo y después el proveedor. Su formulario aparece a la derecha.

3. Selecciona o introduce el **Modelo** y la **Clave API** cuando sea necesaria. Cambia la **URL base de la API** solo para un endpoint personalizado; vacía usa el valor predeterminado.

4. Si el modelo lo admite, elige el **Esfuerzo de razonamiento** o ajusta el presupuesto en **Ajustes avanzados**.

5. Pulsa **Guardar configuración** y espera la confirmación. Para usar el modelo guardado como predeterminado actual, pulsa después **Establecer como modelo actual**.

6. Vuelve a la conversación y comprueba el modelo debajo de la entrada. Ese control permite elegir el modelo de la conversación.

:::note

Guardar la configuración y establecer el modelo actual son acciones distintas. Guarda los campos editados antes de activar el proveedor.

:::

El catálogo y las opciones avanzadas aparecen abajo. Las variables de entorno y archivos manuales se destinan a scripts, servicios o inicios personalizados; consulte [variables](./environment-variables.md) y [archivos](./runtime-configuration.md).

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

Los proveedores que usan el protocolo de OpenAI utilizan Chat Completions de forma predeterminada. El protocolo integrado de un modelo puede ser Responses si sus llamadas a herramientas lo requieren. Los protocolos predeterminados y las restricciones de herramientas de Chat se mantienen en el catálogo integrado de modelos; no se incluye aquí una lista separada. Los proveedores Anthropic conservan su protocolo Messages actual.

Los proveedores que utilizan el protocolo OpenAI pueden seleccionar Responses de forma explícita para cada modelo, incluidos Azure OpenAI, `openai_compatible` y DashScope Token Plan. Configure `apiBase` con la URL base de un servicio que admita Responses. Para Azure OpenAI, utilice la URL base con `/openai/v1/` y establezca `model` en el nombre del despliegue.

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) y [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) documentan la compatibilidad con Responses para determinados endpoints y modelos. En los endpoints compatibles personalizados y los planes de programación, la compatibilidad depende del servicio. [Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) indica expresamente que no admite Responses. Si se selecciona Responses para un servicio o modelo que no lo admite, la solicitud devuelve un error; IaC Code no cambia automáticamente a Chat.

Para usar Responses con un modelo Qwen compatible, añada esta configuración a nivel de modelo en `settings.yml` y sustituya `<model-id>` por el ID real del modelo:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

Cambie `apiMode` a `chat_completions` para volver al protocolo anterior. Si lo omite, se utiliza el protocolo predeterminado del modelo; para los modelos de DashScope estándar sigue siendo Chat Completions. La configuración solo se aplica al modelo indicado dentro del proveedor indicado.

Si selecciona Chat de forma explícita, el modelo debe admitir llamadas a herramientas con el nivel de razonamiento elegido. Utilice Responses cuando la API Chat del modelo no admita esa combinación.

Consulte la [configuración de ejecución](./runtime-configuration.md) para conocer los parámetros de solicitud y los límites de salida.
