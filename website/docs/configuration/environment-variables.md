---
title: Environment Variables
description: All supported environment variables and precedence rules.
---

# Environment Variables

IaC Code reads configuration from CLI arguments, environment variables, and configuration files. The precedence is:

```text
CLI arguments > environment variables > configuration files
```

Environment variables are useful for CI/CD pipelines, containers, and one-off overrides without editing configuration files.

## LLM Configuration

| Variable | Description |
|---|---|
| `IAC_CODE_PROVIDER` | Model provider name (case-insensitive). Valid values: `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAPI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Model name |
| `IAC_CODE_BASE_URL` | API endpoint for `OpenAPI Compatible` and `Anthropic Compatible` only; ignored for other providers |
| `IAC_CODE_API_KEY` | Provider API key; overrides the active provider's key in `.credentials.yml` |

See [LLM Providers](./llm-providers.md) for provider details.

## Alibaba Cloud Credentials

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token; switches the credential mode to STS when set |
| `ALIBABA_CLOUD_REGION_ID` | Default region |

See [Alibaba Cloud Credentials](./alibaba-cloud-credentials.md) for more details.

## Telemetry

| Variable | Description |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Set to `1` / `true` / `yes` / `on` to disable non-essential telemetry traffic |
| `DISABLE_TELEMETRY` | Set to `1` / `true` / `yes` / `on` to disable all telemetry |
| `IAC_CODE_TELEMETRY_ENDPOINT` | Base OTLP endpoint; individual signal endpoints default to this value |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | Override endpoint for traces |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | Override endpoint for metrics |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | Override endpoint for logs |
| `IAC_CODE_TELEMETRY_HEADERS` | Custom OTLP headers (JSON or key=value format) |

## Other

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Override the runtime configuration directory (default `~/.iac-code/`); supports `~` and `$VAR` expansion. All persisted artifacts (credentials, settings, history, projects, image cache, skills, telemetry, etc.) follow it |
| `IAC_CODE_ENV` | Deployment environment label (default: `production`) |
| `IAC_CODE_TENANT_ID` | Tenant identifier for telemetry; auto-prefixed with `iac_tenant_` if not already |
| `IAC_CODE_GIT_BASH_PATH` | Path to Git Bash `bash.exe` on Windows when it is not on PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Environment-managed encrypted push secret keyring for A2A (JSON format) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Standard OpenTelemetry endpoint; when set, enables OTLP export |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capture GenAI message/tool content on spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
