---
title: Umgebungsvariablen
description: Alle unterstuetzten Umgebungsvariablen und Rangfolgeregeln.
---

# Umgebungsvariablen

IaC Code liest die Konfiguration aus CLI-Argumenten, Umgebungsvariablen und Konfigurationsdateien. Die Rangfolge ist:

```text
CLI-Argumente > Umgebungsvariablen > Konfigurationsdateien
```

Umgebungsvariablen sind nuetzlich fuer CI/CD-Pipelines, Container und einmalige Ueberschreibungen, ohne Konfigurationsdateien bearbeiten zu muessen.

## LLM-Konfiguration

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_PROVIDER` | Name des Modellanbieters (Gross-/Kleinschreibung wird nicht beachtet): `Anthropic`, `OpenAI`, `DashScope`, `DashScopeTokenPlan`, `DeepSeek`, `OpenAPICompatible` |
| `IAC_CODE_MODEL` | Modellname |
| `IAC_CODE_BASE_URL` | API-Endpunkt nur fuer `OpenAPICompatible`; wird fuer andere Anbieter ignoriert |
| `IAC_CODE_API_KEY` | API-Schluessel des Anbieters; ueberschreibt den Schluessel des aktiven Anbieters in `.credentials.yml` |

Siehe [LLM-Anbieter](./llm-providers.md) fuer Anbieterdetails.

## Alibaba Cloud-Anmeldedaten

| Variable | Beschreibung |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey-ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey-Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS-Token; wechselt den Anmeldedatenmodus zu STS, wenn gesetzt |
| `ALIBABA_CLOUD_REGION_ID` | Standardregion |

Siehe [Alibaba Cloud-Anmeldedaten](./alibaba-cloud-credentials.md) fuer weitere Details.

## Telemetrie

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Auf `1` / `true` / `yes` / `on` setzen, um nicht-essentiellen Telemetrie-Datenverkehr zu deaktivieren |
| `DISABLE_TELEMETRY` | Auf `1` / `true` / `yes` / `on` setzen, um die gesamte Telemetrie zu deaktivieren |
| `IAC_CODE_TELEMETRY_ENDPOINT` | Basis-OTLP-Endpunkt; einzelne Signalendpunkte verwenden standardmaessig diesen Wert |
| `IAC_CODE_TELEMETRY_TRACES_ENDPOINT` | Ueberschreibungsendpunkt fuer Traces |
| `IAC_CODE_TELEMETRY_METRICS_ENDPOINT` | Ueberschreibungsendpunkt fuer Metriken |
| `IAC_CODE_TELEMETRY_LOGS_ENDPOINT` | Ueberschreibungsendpunkt fuer Protokolle |
| `IAC_CODE_TELEMETRY_HEADERS` | Benutzerdefinierte OTLP-Header (JSON- oder key=value-Format) |

## Sonstiges

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Ueberschreibt das Laufzeitkonfigurationsverzeichnis (Standard `~/.iac-code/`); unterstuetzt `~`- und `$VAR`-Erweiterung. Alle persistierten Artefakte (Anmeldedaten, Einstellungen, Verlauf, projects, image-cache, skills, telemetry usw.) folgen diesem Verzeichnis |
| `IAC_CODE_ENV` | Bezeichnung der Bereitstellungsumgebung (Standard: `production`) |
| `IAC_CODE_TENANT_ID` | Mandantenkennung fuer Telemetrie; wird automatisch mit `iac_tenant_` vorangestellt, wenn nicht bereits vorhanden |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Standard-OpenTelemetry-Endpunkt; aktiviert den OTLP-Export, wenn gesetzt |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | GenAI-Nachrichten-/Tool-Inhalte auf Spans erfassen: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |
