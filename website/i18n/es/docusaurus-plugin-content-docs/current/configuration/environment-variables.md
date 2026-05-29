---
title: Variables de entorno
description: Todas las variables de entorno soportadas y reglas de precedencia.
---

# Variables de entorno

IaC Code lee la configuracion desde los argumentos del CLI, las variables de entorno y los archivos de configuracion. La precedencia es:

```text
CLI arguments > environment variables > configuration files
```

Las variables de entorno son utiles para pipelines de CI/CD, contenedores y sobreescrituras puntuales sin editar archivos de configuracion.

## Configuracion de LLM

| Variable | Descripcion |
|---|---|
| `IAC_CODE_PROVIDER` | Nombre del proveedor de modelos (sin distincion de mayusculas/minusculas). Valores validos: `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAPI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Nombre del modelo |
| `IAC_CODE_BASE_URL` | Endpoint de API para `OpenAPI Compatible` y `Anthropic Compatible` solamente; se ignora para otros proveedores |
| `IAC_CODE_API_KEY` | Clave API del proveedor; sobreescribe la clave del proveedor activo en `.credentials.yml` |

Consulta [Proveedores de LLM](./llm-providers.md) para mas detalles sobre los proveedores.

## Credenciales de Alibaba Cloud

| Variable | Descripcion |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Token STS; cambia el modo de credenciales a STS cuando se establece |
| `ALIBABA_CLOUD_REGION_ID` | Region predeterminada |

Consulta [Credenciales de Alibaba Cloud](./alibaba-cloud-credentials.md) para mas detalles.

## Telemetria

| Variable | Descripcion |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Establecer en `1` / `true` / `yes` / `on` para deshabilitar el trafico de telemetria no esencial |
| `DISABLE_TELEMETRY` | Establecer en `1` / `true` / `yes` / `on` para deshabilitar toda la telemetria |
| `IAC_CODE_TELEMETRY_ENDPOINT` | Endpoint base de OTLP; los endpoints de senales individuales usan este valor por defecto |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | Endpoint sobreescrito para trazas |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | Endpoint sobreescrito para metricas |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | Endpoint sobreescrito para registros |
| `IAC_CODE_TELEMETRY_HEADERS` | Encabezados OTLP personalizados (formato JSON o clave=valor) |

## Otros

| Variable | Descripcion |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Sobreescribe el directorio de configuracion en tiempo de ejecucion (predeterminado `~/.iac-code/`); admite expansion de `~` y `$VAR`. Todos los artefactos persistidos (credenciales, ajustes, historial, projects, image-cache, skills, telemetry, etc.) siguen este directorio |
| `IAC_CODE_ENV` | Etiqueta del entorno de despliegue (predeterminado: `production`) |
| `IAC_CODE_TENANT_ID` | Identificador de tenant para telemetria; se le agrega automaticamente el prefijo `iac_tenant_` si no lo tiene |
| `IAC_CODE_GIT_BASH_PATH` | Ruta a `bash.exe` de Git Bash en Windows cuando no esta en el PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Keyring de secretos push A2A cifrados gestionado por el entorno (formato JSON) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint estandar de OpenTelemetry; cuando se establece, habilita la exportacion OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturar contenido de mensajes/herramientas de GenAI en spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
