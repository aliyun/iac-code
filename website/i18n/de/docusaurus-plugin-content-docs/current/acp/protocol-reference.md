---
title: Protokollreferenz
description: Vollstaendige Referenz der ACP-Protokollmethoden und -Ereignisse fuer die iac-code-Integration.
sidebar_position: 3
---

# Protokollreferenz

Dieses Dokument bietet eine vollstaendige Referenz fuer die ACP-Methoden (Agent Client Protocol) und Streaming-Ereignisse, die vom iac-code-Server bereitgestellt werden.

## Lebenszyklus-Uebersicht

Eine typische ACP-Sitzung folgt diesem Ablauf:

```
initialize → new_session → prompt (loop) → close_session
                ↑                              │
                └── load_session / resume ──────┘
```

1. **initialize** -- Handshake. Protokollversion aushandeln und Serverfaehigkeiten ermitteln.
2. **session/new** -- Eine neue Sitzung mit einer unabhaengigen Agenten-Laufzeitumgebung erstellen.
3. **session/prompt** -- Benutzereingabe senden; Streaming-Ereignisse bis zur endgueltigen Antwort empfangen.
4. **session/close** -- Die Sitzung und ihre Ressourcen freigeben.

Sitzungen koennen auch aus dem Verlauf geladen (`session/load`) oder fortgesetzt (`session/resume`) werden, anstatt neue zu erstellen.

---

## Methoden

### initialize

Protokoll-Handshake. Muss der erste Aufruf bei jeder Verbindung sein.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `protocolVersion` | integer | Ja | Angeforderte Protokollversion (derzeit `1`) |
| `clientInfo` | object | Nein | Clientname und -version |
| `clientCapabilities` | object | Nein | Vom Client unterstuetzte Faehigkeiten |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `protocolVersion` | integer | Ausgehandelte Protokollversion |
| `agentCapabilities` | object | Serverfaehigkeiten (siehe unten) |
| `agentInfo` | object | Servername und -version |
| `authMethods` | array | Verfuegbare Authentifizierungsmethoden (leer bei Verwendung eingebauter Anmeldedaten) |

**Agentenfaehigkeiten**

| Faehigkeit | Wert | Bedeutung |
|-----------|-------|---------|
| `loadSession` | `true` | Unterstuetzt das Wiederherstellen von Sitzungen aus dem Verlauf |
| `promptCapabilities.embeddedContext` | `true` | Akzeptiert eingebettete Ressourceninhalte in Eingaben |
| `promptCapabilities.image` | `false` | Bildeingabe nicht unterstuetzt (wird zu Textmarker degradiert) |
| `promptCapabilities.audio` | `false` | Audioeingabe nicht unterstuetzt (wird zu Textmarker degradiert) |
| `sessionCapabilities.list` | `{}` | Unterstuetzt das Auflisten von Sitzungen |
| `sessionCapabilities.close` | `{}` | Unterstuetzt das Schliessen von Sitzungen |

---

### session/new

Erstellen Sie eine neue Sitzung mit einer unabhaengigen Agenten-Laufzeitumgebung, Tool-Registry und LLM-Kontext.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `cwd` | string | Ja | Absoluter Pfad zum Arbeitsverzeichnis |
| `mcpServers` | object | Nein | MCP-Serverkonfiguration (akzeptiert, aber noch nicht funktional) |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `sessionId` | string | Eindeutiger Sitzungsbezeichner fuer nachfolgende Aufrufe |
| `modes` | object | Verfuegbare Modi und aktueller Modus |
| `models` | object | Verfuegbare Modelle und aktuelles Modell |

:::note
Jede Sitzung erstellt eine unabhaengige AgentLoop. Mehrere Sitzungen koennen gleichzeitig laufen, aber jede verbraucht eine LLM-Verbindung.
:::

---

### session/load

Laden Sie eine zuvor gespeicherte Sitzung von der Festplatte und stellen Sie ihren Nachrichtenverlauf wieder her.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `cwd` | string | Ja | Pfad zum Arbeitsverzeichnis |
| `sessionId` | string | Ja | ID der zu ladenden Sitzung |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `models` | object | Verfuegbare Modelle und aktueller Modellzustand |
| `modes` | object | Verfuegbare Modi und aktueller Moduszustand |

:::note
Das Laden einer Sitzung liest den Verlauf aus `~/.iac-code/sessions/`, repariert automatisch unterbrochene Nachrichten und injiziert den Verlauf in eine neue AgentLoop.
:::

---

### session/fork

Verzweigen Sie eine bestehende Sitzung, um einen unabhaengigen Zweig mit demselben Verlauf zu erstellen.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `cwd` | string | Ja | Pfad zum Arbeitsverzeichnis |
| `sessionId` | string | Ja | ID der zu verzweigenden Sitzung |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `sessionId` | string | Neue Sitzungs-ID fuer den verzweigten Zweig |
| `models` | object | Verfuegbare Modelle und aktueller Modellzustand |
| `modes` | object | Verfuegbare Modi und aktueller Moduszustand |

---

### session/resume

Setzen Sie eine bestehende Sitzung fort oder verbinden Sie sich erneut. Der Verlauf wird bei Bedarf automatisch geladen.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `cwd` | string | Ja | Pfad zum Arbeitsverzeichnis |
| `sessionId` | string | Ja | ID der fortzusetzenden Sitzung |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `models` | object | Verfuegbare Modelle und aktueller Modellzustand (optional) |
| `modes` | object | Verfuegbare Modi und aktueller Moduszustand (optional) |

:::note
Im Gegensatz zu `session/new` enthaelt die Antwort kein `sessionId`-Feld, da der Client die Sitzungs-ID bereits aus der Anfrage kennt.
:::

---

### session/prompt

Senden Sie Benutzereingaben und loesen Sie Streaming-Agentenantworten aus.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `sessionId` | string | Ja | Ziel-Sitzungs-ID |
| `prompt` | array | Ja | Array von Inhaltsbloecken (siehe Inhaltsblocktypen unten) |

**Inhaltsblocktypen**

| Typ | Beschreibung |
|------|-------------|
| `TextContentBlock` | Klartext-Benutzereingabe |
| `EmbeddedResourceContentBlock` | Inline eingebetteter Dateiinhalt |
| `ResourceContentBlock` | Ressourcen-Link-Referenz |
| `ImageContentBlock` | Bild (wird zu `[image: mime/type]`-Textmarker degradiert) |
| `AudioContentBlock` | Audio (wird zu `[audio: mime/type]`-Textmarker degradiert) |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `stopReason` | string | Warum die Eingabe abgeschlossen wurde (siehe Abschlussgruende) |
| `usage` | object | Token-Nutzung: `inputTokens`, `outputTokens`, `totalTokens` |

**Abschlussgruende**

| Wert | Bedeutung |
|-------|---------|
| `end_turn` | Modell hat normal abgeschlossen |
| `max_turn_requests` | Maximale Tool-Aufruf-Schleifengrenze erreicht |
| `max_tokens` | Ausgabe-Token-Limit erreicht |
| `cancelled` | Client hat die Eingabe abgebrochen |
| `refusal` | Modell hat die Antwort verweigert |

:::note
Waehrend der Ausfuehrung sendet der Server `session/update`-Benachrichtigungen mit Streaming-Ereignissen, bevor er die endgueltige Antwort zurueckgibt.
:::

---

### session/cancel

Brechen Sie eine laufende Eingabeaufgabe ab.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `sessionId` | string | Ja | Sitzung mit der laufenden Eingabe |

**Verhalten**

- Stoppt den Verbrauch von Stream-Ereignissen
- Laufende Tools werden nicht zwangsweise beendet, aber Ergebnisse werden verworfen
- Der ausstehende `prompt`-Aufruf gibt mit `stopReason: "cancelled"` zurueck

---

### session/close

Schliessen Sie eine Sitzung und geben Sie ihre Ressourcen frei.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `sessionId` | string | Ja | Zu schliessende Sitzung |

**Verhalten**

- Sitzung wird aus dem Speicher entfernt
- Gespeicherter Verlauf bleibt auf der Festplatte
- Nachfolgende `prompt`-Aufrufe an diese Sitzung geben einen Fehler zurueck

---

### sessions/list

Listen Sie alle gespeicherten Sitzungen fuer ein gegebenes Arbeitsverzeichnis auf.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `cwd` | string | Ja | Arbeitsverzeichnis zur Eingrenzung der Auflistung |

**Antwortfelder**

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `sessions` | array | Liste von Sitzungsobjekten mit `sessionId` und Metadaten |

---

### config/set

Setzen Sie dynamisch eine Konfigurationsoption fuer eine Sitzung.

**Anfrageparameter**

| Feld | Typ | Erforderlich | Beschreibung |
|-------|------|----------|-------------|
| `sessionId` | string | Ja | Zielsitzung |
| `configId` | string | Ja | Zu setzender Konfigurationsschluessel |
| `value` | any | Ja | Neuer Wert |

---

## Streaming-Ereignisse

Waehrend der Ausfuehrung von `session/prompt` sendet der Server `session/update`-Benachrichtigungen mit Streaming-Ereignisdaten.

### Ereignisformat

Jede `session/update`-Benachrichtigung traegt ein Update-Objekt mit einem bestimmten Typ:

```json
{
  "jsonrpc": "2.0",
  "method": "session/update",
  "params": {
    "sessionId": "abc123",
    "update": { "type": "agent_message_chunk", "text": "..." }
  }
}
```

### Ereignistyp-Zuordnung

| Internes Ereignis | ACP-Update-Typ | Beschreibung |
|---------------|----------------|-------------|
| `TextDeltaEvent` | `AgentMessageChunk` | Inkrementelle Agenten-Textausgabe |
| `ThinkingDeltaEvent` | `AgentThoughtChunk` | Modell-Denk-/Ueberlegungsinhalt |
| `ToolUseStartEvent` | `ToolCallStart` | Tool-Aufruf beginnt |
| `ToolResultEvent` | `ToolCallProgress` | Tool-Ergebnis (abgeschlossen oder fehlgeschlagen) |
| `CompactionEvent` | `AgentMessageChunk` | Kontextkomprimierungsbenachrichtigung |
| `ErrorEvent` | `AgentMessageChunk` | Fehlerinformation |

### Tool-Aufruf-Lebenszyklus

```
ToolCallStart (status=in_progress)
    │
    ├── ToolCallProgress (status=in_progress, Eingabezusammenfassung / sichere Prompt-Eingabe)
    │
    ├── ToolCallProgress (status=completed, raw_output=result)   ← success
    │
    └── ToolCallProgress (status=failed, raw_output=error)       ← failure
```

**Tool-Art-Zuordnung**

| Tool | ACP ToolKind |
|------|-------------|
| `read_file`, `list_files` | `read` |
| `glob`, `grep` | `search` |
| `write_file`, `edit_file` | `edit` |
| `bash`, `agent` | `execute` |
| `web_fetch` | `fetch` |
| Sonstige | `other` |

---

## Berechtigungsanfragen

Vor der Ausfuehrung risikoreicher Tools sendet iac-code einen `request_permission`-Callback an den Client.

### Tool-Berechtigungskategorien

| Kategorie | Tools | Automatisch erlaubt |
|----------|-------|-------------|
| Nur-Lese | `read_file`, `list_files`, `glob`, `grep`, `web_fetch` | Ja |
| Nur-Lese-Cloud-API | Als nur lesend eingestufte `aliyun_api`-Aktionen | Ja |
| Schreiben | `write_file`, `edit_file` | Nein -- erfordert Genehmigung |
| Ausfuehren | `bash`, `agent` | Nein -- erfordert Genehmigung |
| Cloud-Schreib-API | Nicht nur lesende `aliyun_api`-Aufrufe | Nein -- erfordert Genehmigung pro API |

### request_permission-Ereignis

Der Server sendet einen `request_permission`-Callback mit:

| Feld | Typ | Beschreibung |
|-------|------|-------------|
| `options` | array | Verfuegbare Berechtigungsoptionen |
| `sessionId` | string | Sitzung, die Berechtigung anfordert |
| `toolCall` | object | Tool-Aufruf-Details (Titel, Art, Eingabe) |

### Berechtigungsoptionen

| Options-ID | Bedeutung |
|-----------|---------|
| `allow_once` | Diesen spezifischen Aufruf erlauben |
| `allow_always` | Alle zukuenftigen Aufrufe dieses Tools in dieser Sitzung erlauben, wenn das Tool blanket allow unterstuetzt; fuer `bash` und `aliyun_api` standardmaessig nicht angeboten |
| `allow_rule:<rules>` | Zukuenftige Aufrufe erlauben, die den vorgeschlagenen Regeln in dieser Sitzung entsprechen |
| `deny_rule:<rules>` | Zukuenftige Aufrufe verweigern, die den vorgeschlagenen Regeln in dieser Sitzung entsprechen |
| `reject_once` | Diesen spezifischen Aufruf verweigern |
| `reject_always` | Alle zukuenftigen Aufrufe dieses Tools in dieser Sitzung verweigern |

Fuer `aliyun_api` werden Nur-Lese-Aktionen automatisch erlaubt. Nicht nur lesende RPC- und ROA-Aktionen koennen eine exakte Regel wie `aliyun_api(ros:CreateStack)` oder `aliyun_api(cs:CreateCluster)` anbieten. Wildcard-Allow-Regeln genehmigen nicht nur lesende Aufrufe weiterhin nicht.

### Antwortformat

```json
{
  "outcome": "allowed",
  "option_id": "allow_once"
}
```

Oder zum Verweigern:

```json
{
  "outcome": "denied"
}
```

| Client-Antwort | Tool-Verhalten |
|----------------|---------------|
| `AllowedOutcome` | Tool wird normal ausgefuehrt |
| `DeniedOutcome` | Tool wird uebersprungen; Modell erhaelt Fehlermeldung "Permission denied." |

---

## Fehlerbehandlung

### RequestError-Format

Fehler folgen dem JSON-RPC 2.0-Fehlerformat:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {"session_id": "Session not found"}
  }
}
```

### Haeufige Fehlercodes

| Code | Name | Beschreibung |
|------|------|-------------|
| `-32700` | Parse error | Ungueltiges JSON |
| `-32600` | Invalid request | Fehlerhaftes JSON-RPC |
| `-32601` | Method not found | Unbekannte Methode |
| `-32602` | Invalid params | Fehlende oder ungueltige Parameter (z.B. unbekannte Sitzungs-ID) |
| `-32603` | Internal error | Serverseitiger Fehler |
