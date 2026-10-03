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
| `IAC_CODE_BASE_URL` | Überschreibt den API-Endpunkt des aktiven Anbieters; hat Vorrang vor dem gespeicherten `apiBase` und der integrierten Standard-URL |
| `IAC_CODE_API_KEY` | API-Schluessel des Anbieters |

## Responses API

Anbieter mit OpenAI-Protokoll verwenden standardmäßig Chat Completions. Die registrierten OpenAI-Modelle `gpt-6-astra`, `gpt-6-sol` und `gpt-6-luna` verwenden standardmäßig die Responses API. Anthropic-Anbieter verwenden weiterhin ihr bisheriges Messages-Protokoll.

Responses ist derzeit für den offiziellen OpenAI-Endpunkt sowie geprüfte Endpunkte und Modelle von Standard-DashScope verfügbar, etwa `qwen3.8-max`. Azure OpenAI, `openai_compatible`, DashScope Token Plan und CodingPlan-Anbieter unterstützen diesen Wechsel nicht.

Um ein Qwen-Modell mit Responses zu verwenden, ergänzen Sie diese Modelleinstellung in `settings.yml`:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: qwen3.8-max
    models:
      qwen3.8-max:
        apiMode: responses
```

Setzen Sie `apiMode` auf `chat_completions`, um zurückzuwechseln. Ohne diesen Eintrag gilt das integrierte Standardprotokoll des Modells; für Modelle von Standard-DashScope bleibt dies Chat Completions. Die Einstellung gilt nur für das angegebene Modell beim angegebenen Anbieter.

Wenn Sie für GPT-6 ausdrücklich Chat wählen, kann Astra keine Werkzeuge aufrufen; Sol und Luna können dies nur mit `effort: none`. Verwenden Sie Responses für Werkzeugaufrufe mit aktiviertem Reasoning.

Anfrageeinstellungen und Ausgabelimits finden Sie unter [Laufzeitkonfiguration](./runtime-configuration.md).
