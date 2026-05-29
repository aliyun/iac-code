---
title: LLM-Anbieter
description: Unterstuetzte Modellanbieter und Umgebungsvariablen.
---

# LLM-Anbieter

IaC Code unterstuetzt mehrere Modellanbieter-Backends. Die Anbieterauswahl kann ueber CLI-Optionen, Umgebungsvariablen oder Konfigurationsdateien erfolgen. Die Rangfolge ist:

```text
CLI-Argumente > Umgebungsvariablen > Konfigurationsdateien
```

## Cloud-Anbieter

| Anbieterwert | Zweck |
|---|---|
| `DashScope` | Alibaba Cloud DashScope (Bailian) kompatibler Endpunkt |
| `DashScope Token Plan` | Alibaba Cloud DashScope Token Plan Endpunkt |
| `OpenAI` | OpenAI-Modelle |
| `Anthropic` | Anthropic-Modelle |
| `DeepSeek` | DeepSeek-Modelle |
| `Gemini` | Google Gemini-Modelle |
| `Azure OpenAI` | Azure OpenAI-Dienst |
| `ModelScope` | ModelScope-Inferenz-Endpunkt |

## Anbieter in China

| Anbieterwert | Zweck |
|---|---|
| `Kimi CN` | Kimi (Moonshot AI) China-Endpunkt |
| `MiniMax CN` | MiniMax China-Endpunkt |
| `ZhiPu CN` | ZhiPu AI (GLM) China-Endpunkt |
| `Volcengine CN` | Volcengine (ByteDance) China-Endpunkt |
| `SiliconFlow CN` | SiliconFlow China-Endpunkt |

## Internationale Anbieter

| Anbieterwert | Zweck |
|---|---|
| `Kimi Intl` | Kimi (Moonshot AI) internationaler Endpunkt |
| `MiniMax Intl` | MiniMax internationaler Endpunkt |
| `ZhiPu Intl` | ZhiPu AI (GLM) internationaler Endpunkt |
| `SiliconFlow Intl` | SiliconFlow internationaler Endpunkt |

## CodingPlan-Anbieter

| Anbieterwert | Zweck |
|---|---|
| `Aliyun CodingPlan` | Alibaba Cloud CodingPlan-Endpunkt |
| `Aliyun CodingPlan Intl` | Alibaba Cloud CodingPlan internationaler Endpunkt |
| `ZhiPu CN CodingPlan` | ZhiPu AI CodingPlan China-Endpunkt |
| `ZhiPu Intl CodingPlan` | ZhiPu AI CodingPlan internationaler Endpunkt |
| `Volcengine CodingPlan` | Volcengine CodingPlan-Endpunkt |

## Kompatibel / Benutzerdefinierte Endpunkte

| Anbieterwert | Zweck |
|---|---|
| `OpenAPI Compatible` | Beliebiger OpenAI-kompatibler API-Endpunkt |
| `Anthropic Compatible` | Beliebiger Anthropic-kompatibler API-Endpunkt |
| `OpenRouter` | OpenRouter-Aggregations-Gateway |

## Lokale Anbieter

| Anbieterwert | Zweck |
|---|---|
| `Ollama` | Ollama lokaler Modellserver |
| `LM Studio` | LM Studio lokaler Modellserver |

## LLM-Umgebungsvariablen

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_PROVIDER` | Name des Modellanbieters (Gross-/Kleinschreibung wird nicht beachtet). Gueltige Werte siehe obige Tabellen |
| `IAC_CODE_MODEL` | Modellname |
| `IAC_CODE_BASE_URL` | API-Endpunkt nur fuer `OpenAPI Compatible` und `Anthropic Compatible`; wird fuer andere Anbieter ignoriert |
| `IAC_CODE_API_KEY` | API-Schluessel des Anbieters |
