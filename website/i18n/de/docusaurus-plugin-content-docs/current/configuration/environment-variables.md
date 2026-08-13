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
| `IAC_CODE_PROVIDER` | Name des Modellanbieters (Gross-/Kleinschreibung wird nicht beachtet). Gueltige Werte: `DashScope`, `DashScope Token Plan`, `OpenAI`, `Anthropic`, `DeepSeek`, `Gemini`, `Azure OpenAI`, `ModelScope`, `Kimi CN`, `Kimi Intl`, `MiniMax CN`, `MiniMax Intl`, `ZhiPu CN`, `ZhiPu Intl`, `Volcengine CN`, `SiliconFlow CN`, `SiliconFlow Intl`, `Aliyun CodingPlan`, `Aliyun CodingPlan Intl`, `ZhiPu CN CodingPlan`, `ZhiPu Intl CodingPlan`, `Volcengine CodingPlan`, `OpenAPI Compatible`, `Anthropic Compatible`, `OpenRouter`, `Ollama`, `LM Studio` |
| `IAC_CODE_MODEL` | Modellname |
| `IAC_CODE_BASE_URL` | Überschreibt den API-Endpunkt des aktiven Anbieters; hat Vorrang vor dem gespeicherten `apiBase` und der integrierten Standard-URL |
| `IAC_CODE_API_KEY` | API-Schluessel des Anbieters; ueberschreibt den Schluessel des aktiven Anbieters in `.credentials.yml` |

Siehe [LLM-Anbieter](./llm-providers.md) fuer Anbieterdetails.

## Alibaba Cloud-Anmeldedaten

| Variable | Beschreibung |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey-ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey-Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS-Token; wechselt den Anmeldedatenmodus zu STS, wenn gesetzt |
| `ALIBABA_CLOUD_REGION_ID` | Standardregion |
| `ALIBABA_CLOUD_ECS_METADATA` | Optionaler Name der ECS-RAM-Rolle; wird verwendet, wenn der Modus bereits `EcsRamRole` ist und kein Rollenname gespeichert wurde, wählt den Modus aber nicht selbst aus |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Auf `true` setzen, um Anmeldedaten aus ECS-Instanzmetadaten zu deaktivieren |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Auf `true` setzen, um IMDSv2 zu verlangen und den Rückgriff auf IMDSv1 zu verhindern |

Die ECS-Metadatenvariablen gelten erst, nachdem der Anmeldedatenmodus als `EcsRamRole` konfiguriert wurde. Ein in IaC Code gespeicherter Rollenname hat Vorrang vor `ALIBABA_CLOUD_ECS_METADATA`; wenn beide fehlen, wird der Rollenname über IMDS ermittelt.

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
| `IAC_CODE_CHANNEL` | Stabiler Telemetrie-Quellkanal mit niedriger Kardinalitaet (Standard: `unknown`), zum Beispiel `ros_official` oder `partner_acme` |

## Sonstiges

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_CONFIG_DIR` | Ueberschreibt das Laufzeitkonfigurationsverzeichnis (Standard `~/.iac-code/`); unterstuetzt `~`- und `$VAR`-Erweiterung. Alle persistierten Artefakte (Anmeldedaten, Einstellungen, Verlauf, projects, image-cache, skills, telemetry usw.) folgen diesem Verzeichnis |
| `IAC_CODE_LOG_DIR` | Ueberschreibt das lokale Verzeichnis fuer Start-/Debug-Logs (Standard `<config-dir>/logs/`); unterstuetzt `~`- und `$VAR`-Erweiterung. Berechtigungsauditdatensaetze folgen dem Sitzungslayout und werden durch diese Variable nicht verschoben |
| `IAC_CODE_PERMISSION_AUDIT_INCLUDE_TOOL_INPUT` | Ueberschreibt `permissions.audit.include_tool_input`; auf `1` / `true` / `yes` / `on` setzen, um die Form der Tool-Eingabe in Berechtigungsauditdatensaetze aufzunehmen, mit Typ/Laenge/Fingerprint statt roher fachlicher Payload-Strings und mit Fingerprints fuer Feldnamen ausserhalb der Whitelist |
| `IAC_CODE_ENV` | Bezeichnung der Bereitstellungsumgebung (Standard: `production`) |
| `IAC_CODE_TENANT_ID` | Mandantenkennung fuer Telemetrie; wird automatisch mit `iac_tenant_` vorangestellt, wenn nicht bereits vorhanden |
| `IAC_CODE_GIT_BASH_PATH` | Pfad zu Git Bash `bash.exe` unter Windows, wenn nicht im PATH |
| `IAC_CODE_A2A_PUSH_KEYRING` | Umgebungsgesteuerter verschluesselter A2A-Push-Secret-Keyring (JSON-Format) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Standard-OpenTelemetry-Endpunkt; aktiviert den OTLP-Export, wenn gesetzt |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | GenAI-Nachrichten-/Tool-Inhalte auf Spans erfassen: `SPAN_ONLY`, `EVENT_ONLY`, `SPAN_AND_EVENT` |


## Sitzungsbackup

| Variable | Beschreibung |
|---|---|
| `IAC_CODE_CONFIG_BACKUP_DIR` | Optionales Verzeichnis fuer Sitzungsbackups; unterstuetzt `~`- und `$VAR`-Expansion sowie `%VAR%`-Expansion unter Windows. In PowerShell eine konkrete Pfadangabe uebergeben oder `$env:VAR` vor dem Start von `iac-code` durch die Shell expandieren lassen. In Sandbox-Deployments ist dies haeufig ein gemounteter OSS-Pfad, muss aber unabhaengig von `IAC_CODE_CONFIG_DIR` und jeder Sitzungsquelle sein, darf sich nicht ueberschneiden und sollte fuer kritische Checkpoints niedrige Latenz bieten. UNC-Pfade, gemappte Laufwerke und gemountete OSS-Pfade muessen `.backup-lock`-Dateisperren, atomare Replace-Semantik und Dateimetadaten ausreichend fuer inkrementelles Spiegeln erhalten; vermeiden Sie Symlink-, Junction- oder reparse-point-Vorfahren fuer die aktive Sitzungsquelle, den Backup-Root und gespiegelte Sitzungen. Wenn aktiviert, spiegeln Checkpoints jede v2-Sitzung nach `<backup>/projects/<project>/<session_id>/` und behalten die gleiche Verzeichnisstruktur wie die aktive Sitzung bei; `.backup-state.json` und `.backup-lock` bleiben lokal und werden nicht kopiert. Normale Chat-Turn-End-Backups verwenden `normal_turn_end` und blockieren die Antwort nicht; nur Fehler bei `critical=true`-Checkpoints blockieren die Veroeffentlichung. Gemeinsame A2A task/context-Indizes koennen separat gemountet werden. |
| `IAC_CODE_CONFIG_BACKUP_TMP_DIR` | Optionales lokales Zwischenverzeichnis fuer `iac-code a2a`; erfordert `IAC_CODE_CONFIG_BACKUP_DIR`. A2A-Backups blockieren nur bis ein unveraenderlicher Snapshot `<session_id>_vX` lokal fertig ist. Danach kopiert ein eigener Prozess die Snapshots in Versionsreihenfolge ins endgueltige Backup-Verzeichnis und loescht sie nach Erfolg. Beide Pfade muessen absolut sein, duerfen sich nicht ueberschneiden, und das Zwischenverzeichnis muss ausserhalb von `IAC_CODE_CONFIG_DIR` liegen. Andere Ausfuehrungsmodi ignorieren diese Einstellung. |
