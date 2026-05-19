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
| `IAC_CODE_PROVIDER` | Nome do provedor de modelo (insensivel a maiusculas e minusculas): `Anthropic`, `OpenAI`, `DashScope`, `DashScopeTokenPlan`, `DeepSeek`, `OpenAPICompatible` |
| `IAC_CODE_MODEL` | Nome do modelo |
| `IAC_CODE_BASE_URL` | Endpoint de API apenas para `OpenAPICompatible`; ignorado para outros provedores |
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
| `IAC_CODE_ENV` | Rotulo do ambiente de implantacao (padrao: `production`) |
| `IAC_CODE_TENANT_ID` | Identificador de tenant para telemetria; prefixado automaticamente com `iac_tenant_` se ainda nao estiver |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint padrao do OpenTelemetry; quando definido, habilita a exportacao OTLP |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capturar conteudo de mensagens/ferramentas GenAI em spans: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
