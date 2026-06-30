---
title: Variaveis de ambiente
description: Todas as variaveis de ambiente suportadas e regras de precedencia.
---

# Variaveis de ambiente

O IaC Code le a configuracao a partir de argumentos do CLI, variaveis de ambiente e arquivos de configuracao. A precedencia e:

```text
CLI arguments > environment variables > configuration files
```

As variaveis de ambiente sao uteis para pipelines de CI/CD, containers e substituicoes pontuais sem editar arquivos de configuracao.

## Configuracao de LLM

| Variavel | Descricao |
|---|---|
| `IAC_CODE_PROVIDER` | Nome do provedor de modelo (insensivel a maiusculas e minusculas). Valores validos: `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAPI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Nome do modelo |
| `IAC_CODE_BASE_URL` | Endpoint de API para `OpenAPI Compatible` e `Anthropic Compatible` apenas; ignorado para outros provedores |
| `IAC_CODE_API_KEY` | Chave de API do provedor; substitui a chave do provedor ativo em `.credentials.yml` |

Consulte [Provedores de LLM](./llm-providers.md) para detalhes sobre os provedores.

## Credenciais da Alibaba Cloud

| Variavel | Descricao |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | Token STS; muda o modo de credencial para STS quando definido |
| `ALIBABA_CLOUD_REGION_ID` | Regiao padrao |

Consulte [Credenciais da Alibaba Cloud](./alibaba-cloud-credentials.md) para mais detalhes.

## Telemetria

| Variavel | Descricao |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Defina como `1` / `true` / `yes` / `on` para desabilitar o trafego de telemetria nao essencial |
| `DISABLE_TELEMETRY` | Defina como `1` / `true` / `yes` / `on` para desabilitar toda a telemetria |
| `IAC_CODE_TELEMETRY_ENDPOINT` | Endpoint OTLP base; os endpoints de sinais individuais usam este valor como padrao |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | Endpoint personalizado para traces |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | Endpoint personalizado para metricas |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | Endpoint personalizado para logs |
| `IAC_CODE_TELEMETRY_HEADERS` | Cabecalhos OTLP personalizados (formato JSON ou chave=valor) |

## Outros

| Variavel | Descricao |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Substitui o diretorio de configuracao em tempo de execucao (padrao `~/.iac-code/`); suporta expansao de `~` e `$VAR`. Todos os artefatos persistidos (credenciais, configuracoes, historico, projects, image-cache, skills, telemetry, etc.) seguem este diretorio |
| `IAC_CODE_LOG_DIR` | Substitui o diretório local de logs de inicialização/depuração (padrão `<config-dir>/logs/`); suporta expansão de `~` e `$VAR`. Registros de auditoria de permissões continuam em `<config-dir>/logs/permission-audit.jsonl` |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | Substitui `permissions.audit.include_tool_input`; defina como `1` / `true` / `yes` / `on` para incluir entrada de ferramenta apenas em forma estrutural nos registros de auditoria de permissões, usando tipo/tamanho/fingerprint em vez de strings brutas de payload de negócio e aplicando fingerprint a nomes de campo fora da lista permitida |
| `IAC_CODE_ENV` | Rotulo do ambiente de implantacao (padrao: `production`) |
| `IAC_CODE_TENANT_ID` | Identificador de tenant para telemetria; prefixado automaticamente com `iac_tenant_` se ainda nao estiver |
| `IAC_CODE_GIT_BASH_PATH` | Caminho para `bash.exe` do Git Bash no Windows quando nao esta no PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Keyring criptografado de push secrets A2A gerenciado pelo ambiente (formato JSON) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint padrao do OpenTelemetry; quando definido, habilita a exportacao OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturar conteudo de mensagens/ferramentas GenAI em spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
