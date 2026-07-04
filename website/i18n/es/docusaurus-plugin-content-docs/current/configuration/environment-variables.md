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
| `IAC_CODE_LOG_DIR` | Sobrescribe el directorio local de logs de arranque/depuración (predeterminado `<config-dir>/logs/`); admite expansión de `~` y `$VAR`. Los registros de auditoría de permisos siguen el layout de sesión y esta variable no los mueve |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | Sobrescribe `permissions.audit.include_tool_input`; establécelo en `1` / `true` / `yes` / `on` para incluir entrada de herramienta solo con forma en los registros de auditoría de permisos, usando tipo/longitud/huella en vez de cadenas de payload de negocio sin procesar y aplicando huellas a nombres de campo fuera de la lista permitida |
| `IAC_CODE_ENV` | Etiqueta del entorno de despliegue (predeterminado: `production`) |
| `IAC_CODE_TENANT_ID` | Identificador de tenant para telemetria; se le agrega automaticamente el prefijo `iac_tenant_` si no lo tiene |
| `IAC_CODE_GIT_BASH_PATH` | Ruta a `bash.exe` de Git Bash en Windows cuando no esta en el PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Keyring de secretos push A2A cifrados gestionado por el entorno (formato JSON) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint estandar de OpenTelemetry; cuando se establece, habilita la exportacion OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturar contenido de mensajes/herramientas de GenAI en spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |


## Copia de seguridad de sesiones

| Variable | Descripción |
|---|---|
| `IAC_CODE_CONFIG_BACKUP_DIR` | Directorio opcional para copias de seguridad de sesión; admite expansión de `~` y `$VAR`, y expansión `%VAR%` en Windows. En PowerShell, pase una ruta concreta o deje que el shell expanda `$env:VAR` antes de iniciar `iac-code`. En despliegues sandbox suele ser una ruta OSS montada, pero debe ser independiente de `IAC_CODE_CONFIG_DIR` y de cualquier origen de sesión, sin solaparse, y con latencia suficientemente baja para checkpoints críticos. Las rutas UNC, unidades mapeadas y rutas OSS montadas deben conservar el bloqueo de archivo `.backup-lock`, reemplazo atómico y metadatos de archivo para el mirroring incremental; evite ancestros symlink, junction o reparse point en el origen de sesión activo, la raíz de backup y las sesiones reflejadas. Cuando está habilitado, los puntos de control reflejan cada sesión v2 en `<backup>/projects/<project>/<session_id>/` con la misma estructura que la sesión activa; `.backup-state.json` y `.backup-lock` quedan locales y no se copian. Los backups de fin de turno normal usan `normal_turn_end` y no bloquean la respuesta; solo los fallos de checkpoints `critical=true` bloquean la publicación. Los índices A2A task/context compartidos pueden montarse por separado. |
