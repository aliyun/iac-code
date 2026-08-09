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
| `IAC_CODE_PROVIDER` | Model provider name (case-insensitive). Valid values: `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Model name |
| `IAC_CODE_BASE_URL` | API endpoint override for the active provider; takes precedence over the saved `apiBase` and built-in default URL |
| `IAC_CODE_API_KEY` | Provider API key; overrides the active provider's key in `.credentials.yml` |

See [LLM Providers](./llm-providers.md) for provider details.

The effective provider Base URL precedence is: explicit runtime override, `IAC_CODE_BASE_URL`, saved `apiBase`, provider registry default, then SDK default.

## Alibaba Cloud Credentials

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token; switches the credential mode to STS when set |
| `ALIBABA_CLOUD_REGION_ID` | Default region |
| `ALIBABA_CLOUD_ECS_METADATA` | Optional ECS RAM role name used when the configured mode is `EcsRamRole` and no role name is saved; does not select the mode by itself |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Set to `true` to disable ECS instance metadata credentials |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Set to `true` to require IMDSv2 and disable fallback to IMDSv1 |

The ECS metadata variables apply only after the credential mode has been configured as `EcsRamRole`. A role name saved in IaC Code takes precedence over `ALIBABA_CLOUD_ECS_METADATA`; if neither is set, the role name is discovered through IMDS.

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
| `IAC_CODE_CHANNEL` | Stable, low-cardinality telemetry source channel (default: `unknown`), for example `ros_official` or `partner_acme` |

## Other

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Override the runtime configuration directory (default `~/.iac-code/`); supports `~` and `$VAR` expansion. All persisted artifacts (credentials, settings, history, projects, image cache, skills, telemetry, etc.) follow it |
| `IAC_CODE_LOG_DIR` | Override the local startup/debug log directory (default `<config-dir>/logs/`); supports `~` and `$VAR` expansion. Permission audit records follow the session layout and are not moved by this variable |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | Override `permissions.audit.include_tool_input`; set to `1` / `true` / `yes` / `on` to include shape-only tool input in permission audit records, using type/length/fingerprint instead of raw business payload strings and fingerprinting non-whitelisted field names |
| `IAC_CODE_ENV` | Deployment environment label (default: `production`) |
| `IAC_CODE_TENANT_ID` | Tenant identifier for telemetry; auto-prefixed with `iac_tenant_` if not already |
| `IAC_CODE_GIT_BASH_PATH` | Path to Git Bash `bash.exe` on Windows when it is not on PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Environment-managed encrypted push secret keyring for A2A (JSON format) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Standard OpenTelemetry endpoint; when set, enables OTLP export |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capture GenAI message/tool content on spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |


## Session Backup

| Variable | Description |
|---|---|
| `IAC_CODE_CONFIG_BACKUP_DIR` | Optional session backup directory; supports `~` and `$VAR` expansion, and `%VAR%` expansion on Windows. In PowerShell, pass a concrete path or let the shell expand `$env:VAR` before starting `iac-code`. In sandbox deployments this is commonly an OSS-mounted path, but it must be independent from and not overlap `IAC_CODE_CONFIG_DIR` or any session source, and should be low latency enough for critical checkpoints. UNC paths, mapped drives, and mounted OSS paths must preserve `.backup-lock` file locking, atomic replace semantics, and file metadata well enough for incremental mirroring; avoid symlink, junction, or reparse-point ancestry for the active session source, backup root, and mirrored sessions. When enabled, checkpoints mirror each v2 session to `<backup>/projects/<project>/<session_id>/` with the same directory shape as the active session; `.backup-state.json` and `.backup-lock` stay local and are not copied. Normal chat turn-end backups use `normal_turn_end` and do not block the response; only `critical=true` checkpoint failures block publication. Shared A2A task/context indexes can be mounted separately. |
