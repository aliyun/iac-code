---
title: 环境变量
description: 所有支持的环境变量及优先级规则。
---

# 环境变量

IaC Code 从 CLI 参数、环境变量和配置文件读取配置。优先级为：

```text
CLI 参数 > 环境变量 > 配置文件
```

环境变量适用于 CI/CD 流水线、容器场景，以及无需编辑配置文件的一次性覆盖。

## LLM 配置

| 变量 | 说明 |
|---|---|
| `IAC_CODE_PROVIDER` | 模型提供商名称（大小写不敏感）。有效值：`DashScope`、`DashScope Token Plan`、`OpenAI`、`Anthropic`、`DeepSeek`、`Gemini`、`Azure OpenAI`、`ModelScope`、`Kimi CN`、`Kimi Intl`、`MiniMax CN`、`MiniMax Intl`、`ZhiPu CN`、`ZhiPu Intl`、`Volcengine CN`、`SiliconFlow CN`、`SiliconFlow Intl`、`Aliyun CodingPlan`、`Aliyun CodingPlan Intl`、`ZhiPu CN CodingPlan`、`ZhiPu Intl CodingPlan`、`Volcengine CodingPlan`、`OpenAPI Compatible`、`Anthropic Compatible`、`OpenRouter`、`Ollama`、`LM Studio` |
| `IAC_CODE_MODEL` | 模型名称 |
| `IAC_CODE_BASE_URL` | 仅 `OpenAI Compatible` 使用的 API 端点；其他提供商会忽略此值（并给出 warning） |
| `IAC_CODE_API_KEY` | 提供商 API Key；覆盖 `.credentials.yml` 中活跃提供商的密钥 |

详见 [LLM 提供商](./llm-providers.md)。

## 阿里云凭证

| 变量 | 说明 |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token；设置后凭证模式切换为 STS |
| `ALIBABA_CLOUD_REGION_ID` | 默认地域 |

详见 [阿里云凭证](./alibaba-cloud-credentials.md)。

## 遥测

| 变量 | 说明 |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 设为 `1` / `true` / `yes` / `on` 可禁用非必要遥测流量 |
| `DISABLE_TELEMETRY` | 设为 `1` / `true` / `yes` / `on` 可禁用全部遥测 |
| `IAC_CODE_TELEMETRY_ENDPOINT` | OTLP 基础端点；各信号端点默认使用此值 |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | 覆盖 traces 端点 |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | 覆盖 metrics 端点 |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | 覆盖 logs 端点 |
| `IAC_CODE_TELEMETRY_HEADERS` | 自定义 OTLP 请求头（JSON 或 key=value 格式） |

## 其他

| 变量 | 说明 |
|---|---|
| `IAC_CODE_CONFIG_DIR` | 覆盖运行时配置目录（默认 `~/.iac-code/`）；支持 `~` 和 `$VAR` 展开。所有持久化产物（凭证、设置、历史、projects、image-cache、skills、telemetry 等）均会跟随该目录 |
| `IAC_CODE_LOG_DIR` | 覆盖本地启动/调试日志目录（默认 `<config-dir>/logs/`）；支持 `~` 和 `$VAR` 展开。权限审计记录遵循 session layout，不会被此变量移动 |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | 覆盖 `permissions.audit.include_tool_input`；设为 `1` / `true` / `yes` / `on` 时，在权限审计记录中包含仅保留形态的工具输入，使用类型/长度/指纹而不是业务原文，并对非白名单字段名使用指纹 |
| `IAC_CODE_ENV` | 部署环境标签（默认：`production`） |
| `IAC_CODE_TENANT_ID` | 遥测租户标识；如未以 `iac_tenant_` 开头则自动添加前缀 |
| `IAC_CODE_GIT_BASH_PATH` | Windows 下 Git Bash `bash.exe` 的路径（不在 PATH 中时使用） |
| `IAC_CODE_A2A_PUSH_KEYRING` | 由环境管理的 A2A 加密推送密钥环（JSON 格式） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 标准 OpenTelemetry 端点；设置后启用 OTLP 导出 |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | 在 span 上捕获 GenAI 消息/工具内容：`SPAN_ONLY`、`EVENT_ONLY`、`SPAN_AND_EVENT` |


## 会话备份

| 变量 | 说明 |
|---|---|
| `IAC_CODE_CONFIG_BACKUP_DIR` | 可选的会话备份目录，支持 `~` 和 `$VAR` 展开，在 Windows 上也支持 `%VAR%` 展开。PowerShell 中请传入具体路径，或在启动 `iac-code` 前让 shell 展开 `$env:VAR`。在 sandbox 部署中通常指向 OSS 挂载路径，但必须独立于 `IAC_CODE_CONFIG_DIR` 和任何 session 源目录且不能互相重叠，并且关键检查点需要足够低延迟。UNC 路径、映射盘和 OSS 挂载路径必须保留 `.backup-lock` 文件锁、原子替换和文件元数据语义，才能支持增量镜像；活动 session 源目录、备份根目录和镜像 session 路径都应避免符号链接、junction 或 reparse point 祖先路径。启用后，检查点会把每个 v2 会话按原目录结构镜像到 `<backup>/projects/<project>/<session_id>/`；`.backup-state.json` 和 `.backup-lock` 只保留在本地，不会复制。普通对话轮次结束备份使用 `normal_turn_end` 且不阻塞响应；只有 `critical=true` 检查点失败才会阻塞发布。共享 A2A task/context 索引可以单独挂载。 |
