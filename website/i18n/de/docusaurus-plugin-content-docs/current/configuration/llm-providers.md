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

Anbieter mit OpenAI-Protokoll verwenden standardmäßig Chat Completions. Bei Modellen, deren Werkzeugaufrufe dies erfordern, kann Responses als Standardprotokoll hinterlegt sein. Das Standardprotokoll und die Einschränkungen für Chat-Werkzeugaufrufe werden im integrierten Modellkatalog gepflegt; eine separate Modellliste wird hier nicht geführt. Anthropic-Anbieter verwenden weiterhin ihr bisheriges Messages-Protokoll.

Anbieter mit OpenAI-Protokoll können Responses ausdrücklich für einzelne Modelle auswählen, darunter Azure OpenAI, `openai_compatible` und DashScope Token Plan. Tragen Sie in `apiBase` eine Basis-URL ein, deren Dienst Responses unterstützt. Für Azure OpenAI verwenden Sie die Basis-URL mit `/openai/v1/` und geben unter `model` den Bereitstellungsnamen an.

[Azure OpenAI](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle) und [DashScope Token Plan](https://help.aliyun.com/en/model-studio/codex) dokumentieren Responses-Unterstützung für entsprechende Endpunkte und Modelle. Bei benutzerdefinierten kompatiblen Endpunkten und Coding-Tarifen hängt die Unterstützung vom jeweiligen Dienst ab. [Alibaba Cloud Coding Plan](https://help.aliyun.com/en/model-studio/coding-plan-faq) unterstützt Responses ausdrücklich nicht. Wird Responses für einen nicht unterstützten Dienst oder ein solches Modell gewählt, führt die Anfrage zu einem Fehler; IaC Code wechselt nicht automatisch zu Chat.

Um ein unterstütztes Qwen-Modell mit Responses zu verwenden, ergänzen Sie diese Modelleinstellung in `settings.yml` und ersetzen Sie `<model-id>` durch die tatsächliche Modell-ID:

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: <model-id>
    models:
      <model-id>:
        apiMode: responses
```

Setzen Sie `apiMode` auf `chat_completions`, um zurückzuwechseln. Ohne diesen Eintrag gilt das integrierte Standardprotokoll des Modells; für Modelle von Standard-DashScope bleibt dies Chat Completions. Die Einstellung gilt nur für das angegebene Modell beim angegebenen Anbieter.

Wenn Sie Chat ausdrücklich wählen, muss das Modell Werkzeugaufrufe mit der gewählten Reasoning-Stufe unterstützen. Verwenden Sie Responses, wenn die Chat-API des Modells diese Kombination nicht unterstützt.

Anfrageeinstellungen und Ausgabelimits finden Sie unter [Laufzeitkonfiguration](./runtime-configuration.md).
